from __future__ import annotations

from ...runtime.execution_budget import budget_provider_payload, ExecutionBudgetExceeded

from .._knob_reader import provider_knob

from ..activity import emit_provider_activity
from .recovery_attempt import _recovery_retry_delay_ms as _openai_retry_delay_ms
import asyncio
import json
from ..._utils import get_value, maybe_await, to_async_iter
from ..error_evidence import ProviderWireError
from ..errors import (
    can_retry_provider_failure,
    create_provider_incomplete_response_error,
    normalize_unknown_provider_failure,
)
from ..prompt_cache import open_ai_prompt_cache_request_options
from .normalization import (
    _append_text_block,
    _apply_chat_completion_usage,
    _emit_pending_chat_tool_calls,
    _merge_tool_call_name,
)
from .params import (
    build_chat_completions_create_params,
    get_openai_provider_label,
    resolve_openai_base_url,
    supports_openai_chat_completions_reasoning_effort,
)
from .transport import (
    _acquire_openai_client,
    _emit,
    _race_openai_activity,
    _raise_openai_cancelled,
)
from typing import Any



async def send_openai_chat_completions_request(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    # Call-time import: the config package imports providers at module load.
    from ...config._knobs import knob_value

    args = {**(args or {}), **kwargs}
    base_url = resolve_openai_base_url("chat_completions", args.get("baseUrl"))
    provider_label = get_openai_provider_label("chat_completions", base_url)
    max_attempts = max(
        1,
        int(args.get("maxAttempts") or provider_knob("provider.openai_max_attempts")),
    )
    last_failure = None
    turn_session = args.get("turnSession") or args.get("turn_session")
    for attempt in range(1, max_attempts + 1):
        phase = "request_start"
        had_output = False
        had_tool_use = False
        blocks: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        stop_reason = "stop"
        emitted_text = ""
        refusal_text = ""
        emitted_tool_ids: set[str] = set()
        pending_tool_calls: dict[int, dict[str, str]] = {}
        observed_choice_indexes: set[int] = set()
        terminal_choice_indexes: set[int] = set()
        reasoning_content = ""
        active_stream: Any = None
        runtime = args.get("runtime") or {}
        request_start_timeout_ms = int(args.get("requestStartTimeoutMs") or knob_value("provider.request_start_timeout_ms"))
        stream_idle_timeout_ms = int(args.get("streamIdleTimeoutMs") or knob_value("provider.stream_idle_timeout_ms"))

        def incomplete_response(reason: str) -> Any:
            return create_provider_incomplete_response_error(
                provider="openai",
                providerLabel=provider_label,
                mode="chat_completions",
                phase=phase,
                expectedTerminal="a recognized finish_reason for every selected choice",
                reason=reason,
                attempt=attempt,
                hadAssistantOutput=had_output,
                hadToolUse=had_tool_use,
                partialAssistantBytes=len(f"{reasoning_content}{emitted_text}".encode("utf-8")),
                partialToolArgumentBytes=sum(
                    len(item.get("arguments", "").encode("utf-8"))
                    for item in pending_tool_calls.values()
                ),
                openItems=max(
                    len(pending_tool_calls),
                    len(observed_choice_indexes - terminal_choice_indexes),
                ),
            )

        try:
            _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
            client, connection_reused = await _acquire_openai_client(args, base_url)
            emit_provider_activity(runtime, "request_started", attempt=attempt)
            chat = get_value(client, "chat")
            completions = get_value(chat, "completions") if chat else None
            if not completions:
                raise RuntimeError("OpenAI Chat Completions client is unavailable.")
            params = build_chat_completions_create_params(
                args["request"],
                {
                    "includeReasoningEffort": supports_openai_chat_completions_reasoning_effort(base_url),
                    "includeStore": not base_url or supports_openai_chat_completions_reasoning_effort(base_url),
                    "requestOptions": open_ai_prompt_cache_request_options(
                        mode="chat_completions",
                        base_url=base_url,
                        config=args.get("config") or {},
                        request=args["request"],
                        request_options=args.get("requestOptions") or {},
                    ),
                },
            )
            wire_messages = params.get("messages") if isinstance(params.get("messages"), list) else []
            wire_bytes = len(
                json.dumps(wire_messages, separators=(",", ":"), default=str).encode(
                    "utf-8"
                )
            )
            _emit(
                runtime,
                "provider.request_started",
                {
                    "provider": "openai",
                    "provider_label": provider_label,
                    "model": args["request"]["model"],
                    "attempt": attempt,
                    "logical_input_bytes": wire_bytes,
                    "logical_input_items": len(wire_messages),
                    "transmitted_input_bytes": wire_bytes,
                    "transmitted_input_items": len(wire_messages),
                    "input_projection": "full",
                    "previous_response_id_present": False,
                    "connection_reused": connection_reused,
                    "transport": "sse",
                    "chain_reset_reason": "stateless_chat_completions",
                },
            )
            response = await _race_openai_activity(
                maybe_await(get_value(completions, "create")(budget_provider_payload(params, "chat"))),
                timeout_ms=request_start_timeout_ms,
                provider_label=provider_label,
                phase=phase,
                attempt=attempt,
                had_output=had_output,
                had_tool_use=had_tool_use,
                abort_signal=runtime.get("abortSignal"),
                turn_session=turn_session,
            )
            active_stream = response
            if turn_session is not None:
                turn_session.bind_stream(response)
            emit_provider_activity(runtime, "response_headers", attempt=attempt)
            if turn_session is not None:
                turn_session.mark_provider_activity("response_headers")
            phase = "stream_open"
            iterator = to_async_iter(response).__aiter__()
            while True:
                try:
                    _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                    chunk = await _race_openai_activity(
                        iterator.__anext__(),
                        timeout_ms=stream_idle_timeout_ms,
                        provider_label=provider_label,
                        phase=phase,
                        attempt=attempt,
                        had_output=had_output,
                        had_tool_use=had_tool_use,
                        abort_signal=runtime.get("abortSignal"),
                        turn_session=turn_session,
                    )
                except StopAsyncIteration:
                    break
                phase = "stream_read"
                emit_provider_activity(runtime, "stream_frame", attempt=attempt)
                if turn_session is not None:
                    turn_session.mark_provider_activity("stream_frame")
                if runtime.get("streamProviderMessages"):
                    _emit(runtime, "provider.stream_event", {"stream_event": "chat.completion.chunk", "model": args["request"]["model"], "payload": chunk})
                error_payload = get_value(chunk, "error")
                if error_payload:
                    raise _chat_in_band_stream_error(error_payload)
                _apply_chat_completion_usage(usage, get_value(chunk, "usage"))
                raw_choices = get_value(chunk, "choices", [])
                choices = raw_choices if isinstance(raw_choices, list) else []
                for choice_position, choice in enumerate(choices):
                    raw_choice_index = get_value(choice, "index")
                    choice_index = (
                        raw_choice_index
                        if isinstance(raw_choice_index, int) and not isinstance(raw_choice_index, bool)
                        else choice_position
                    )
                    observed_choice_indexes.add(choice_index)
                    delta = get_value(choice, "delta", {}) or {}
                    content_delta = get_value(delta, "content", "")
                    if isinstance(content_delta, str) and content_delta:
                        emit_provider_activity(runtime, "output_delta", attempt=attempt)
                        if turn_session is not None:
                            turn_session.mark_provider_activity("output_delta")
                        had_output = True
                        emitted_text += content_delta
                        if runtime.get("includePartialMessages"):
                            _emit(runtime, "message.assistant.partial", {"session_id": None, "model": args["request"]["model"], "text_delta": content_delta, "text": emitted_text})
                        _append_text_block(blocks, content_delta)
                    refusal_delta = get_value(delta, "refusal", "")
                    if isinstance(refusal_delta, str) and refusal_delta:
                        refusal_text += refusal_delta
                    reasoning_delta = get_value(delta, "reasoning_content", "")
                    if isinstance(reasoning_delta, str) and reasoning_delta:
                        emit_provider_activity(runtime, "reasoning", attempt=attempt)
                        if turn_session is not None:
                            turn_session.mark_provider_activity("reasoning")
                        reasoning_content += reasoning_delta
                        if runtime.get("includePartialMessages"):
                            _emit(
                                runtime,
                                "message.assistant.thinking.partial",
                                {
                                    "session_id": None,
                                    "model": args["request"]["model"],
                                    "thinking_delta": reasoning_delta,
                                    "thinking": reasoning_content,
                                    "reasoning_kind": "raw_thinking",
                                    "user_visible": False,
                                    "provider_event_type": "chat.completion.chunk.reasoning_content",
                                },
                            )
                    raw_tool_calls = get_value(delta, "tool_calls", [])
                    tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else []
                    for default_index, tool_call in enumerate(tool_calls):
                        emit_provider_activity(runtime, "tool_boundary", attempt=attempt)
                        raw_index = get_value(tool_call, "index")
                        index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else default_index
                        pending = pending_tool_calls.setdefault(index, {"arguments": ""})
                        tool_call_id = get_value(tool_call, "id")
                        if isinstance(tool_call_id, str) and tool_call_id:
                            pending["id"] = tool_call_id
                        fn = get_value(tool_call, "function", {}) or {}
                        fn_name = get_value(fn, "name")
                        if isinstance(fn_name, str) and fn_name:
                            pending["name"] = _merge_tool_call_name(pending.get("name"), fn_name)
                        fn_arguments = get_value(fn, "arguments")
                        if isinstance(fn_arguments, str) and fn_arguments:
                            pending["arguments"] += fn_arguments
                    finish_value = get_value(choice, "finish_reason", "")
                    finish = finish_value if isinstance(finish_value, str) else ""
                    if finish and finish not in {"stop", "length", "tool_calls", "function_call", "content_filter"}:
                        raise incomplete_response(
                            f"choice {choice_index} ended with unrecognized finish_reason {finish}"
                        )
                    if finish in ("tool_calls", "function_call"):
                        try:
                            had_tool_use = (
                                _emit_pending_chat_tool_calls(
                                    blocks,
                                    pending_tool_calls,
                                    emitted_tool_ids,
                                    reasoning_content,
                                )
                                or had_tool_use
                            )
                        except Exception as exc:
                            raise incomplete_response(str(exc) or "tool-call assembly failed") from exc
                        if not had_tool_use:
                            raise incomplete_response(
                                f"choice {choice_index} declared tool_calls without a complete tool call"
                            )
                        stop_reason = "tool_calls"
                        terminal_choice_indexes.add(choice_index)
                    elif finish:
                        if pending_tool_calls:
                            raise incomplete_response(
                                f"choice {choice_index} ended while tool-call fragments remained open"
                            )
                        stop_reason = "max_output_tokens" if finish == "length" else finish
                        terminal_choice_indexes.add(choice_index)
            if not observed_choice_indexes:
                raise incomplete_response("the stream ended without a selected choice")
            unterminated = sorted(observed_choice_indexes - terminal_choice_indexes)
            if unterminated:
                joined = ", ".join(str(index) for index in unterminated)
                raise incomplete_response(
                    f"the stream ended before choices {joined} reached a terminal state"
                )
            if pending_tool_calls:
                raise incomplete_response("the stream ended with unfinished tool-call fragments")
            if not blocks and refusal_text:
                _append_text_block(blocks, refusal_text)
                stop_reason = "refusal"
            phase = "response_complete"
            if turn_session is not None:
                await turn_session.release_stream(active_stream)
                active_stream = None
            if attempt > 1:
                _emit(runtime, "provider.recovered", {"provider": "openai", "provider_label": provider_label, "model": args["request"]["model"], "attempts": attempt, "category": last_failure.record.get("category") if last_failure else None})
            return {"content": blocks, "usage": usage, "model": args["request"]["model"], "stopReason": stop_reason}
        except ExecutionBudgetExceeded:
            raise
        except Exception as exc:
            if turn_session is not None and active_stream is not None:
                await turn_session.release_stream(active_stream)
                active_stream = None
            failure = normalize_unknown_provider_failure(
                error=exc,
                provider="openai",
                providerLabel=provider_label,
                phase=phase,
                attempt=attempt,
                hadAssistantOutput=had_output,
                hadToolUse=had_tool_use,
            )
            last_failure = failure
            if can_retry_provider_failure(failure, max_attempts):
                delay_ms = _openai_retry_delay_ms(args, attempt, failure)
                _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                _emit(runtime, "provider.retrying", {**failure.record, "next_attempt": attempt + 1, "delay_ms": delay_ms})
                emit_provider_activity(
                    runtime,
                    "retry_wait",
                    attempt=attempt,
                    force=True,
                )
                await asyncio.sleep(delay_ms / 1000)
                _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                continue
            raise failure
    raise last_failure or RuntimeError("OpenAI Chat Completions request failed unexpectedly.")


def _chat_in_band_stream_error(error_payload: Any) -> ProviderWireError:
    """In-band {'error': …} chunks (Azure/LiteLLM/vLLM/OpenRouter) → evidence."""
    message = (
        str(get_value(error_payload, "message", "") or "")
        or "OpenAI chat stream reported an in-band error."
    )
    code_value = get_value(error_payload, "code")
    status = None
    for candidate in (get_value(error_payload, "status"), code_value):
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and 100 <= candidate <= 599
        ):
            status = candidate
            break
    try:
        body = json.dumps(error_payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        body = str(error_payload)
    error_type = get_value(error_payload, "type")
    return ProviderWireError(
        message,
        status=status,
        body=body,
        code=str(code_value) if code_value is not None else None,
        errorType=str(error_type) if error_type is not None else None,
        streamEventType="error",
    )
