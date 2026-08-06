from __future__ import annotations
from ...contracts.providers import ProviderResponse
from ..partial_output import PartialOutputDelivery
from ..activity import emitProviderActivity
from ..openai_turn_session import PreparedOpenAiRequest as _PreparedOpenAiRequest
from .reconcile import close_openai_output_item as _close_openai_output_item
from .recovery import (
    append_openai_partial,
    create_openai_logical_call_id,
    partial_openai_event_lineage,
    sink_failure,
)
from .recovery_attempt import recover_openai_attempt
from .responses_terminal import handle_response_completed as _handle_response_completed
from .responses_session import (
    extract_turn_state as _extract_turn_state,
    finalize_responses_success as _finalize_responses_success,
    projection_event_fields as _projection_event_fields,
    responses_activity_kind as _responses_activity_kind,
    start_responses_stream as _start_responses_stream,
    stateless_prepared_request as _stateless_prepared_request,
)
from .responses_failures import response_terminal_failure
from .shared import *

_OPENAI_DEFAULT_MAX_ATTEMPTS = 6


async def sendOpenAiRequest(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderResponse:
    args = {**(args or {}), **kwargs}
    if args["mode"] == "chat_completions":
        return await sendOpenAiChatCompletionsRequest(args)
    return await sendOpenAiResponsesRequest(args)
async def sendOpenAiResponsesRequest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    mode = args.get("mode") or _infer_openai_responses_mode(args.get("baseUrl"))
    base_url = resolveOpenAiBaseUrl(mode, args.get("baseUrl"))
    provider_label = getOpenAiProviderLabel(mode, base_url)
    max_attempts = max(
        1,
        int(args.get("maxAttempts") or _OPENAI_DEFAULT_MAX_ATTEMPTS),
    )
    last_failure = None
    turn_session = args.get("turnSession") or args.get("turn_session")
    runtime = args.get("runtime") or {}
    logical_call_id = (
        str(runtime.get("logicalCallId") or "").strip()
        or create_openai_logical_call_id()
    )
    delivery = (
        PartialOutputDelivery(
            runtime,
            logical_call_id,
            session_id=args["request"].get("sessionId"),
        )
        if mode == "codex"
        else None
    )
    for attempt in range(1, max_attempts + 1):
        phase = "request_start"
        had_output = False
        had_tool_use = False
        had_provisional_tool_call = False
        blocks: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        stop_reason = "stop"
        emitted_text = ""
        reasoning_text = ""
        reasoning_summary_text = ""
        raw_reasoning_text = ""
        emitted_tool_ids: set[str] = set()
        pending_by_item_id: dict[str, dict[str, Any]] = {}
        item_to_call_id: dict[str, str] = {}
        item_to_name: dict[str, str] = {}
        open_output_items: dict[str, str] = {}
        completed_output_items: set[str] = set()
        tool_argument_bytes_by_item: dict[str, int] = {}
        item_text: dict[str, str] = {}
        completed_item_order: list[str] = []
        completed_item_records: dict[str, dict[str, Any]] = {}
        open_message_item: str | None = None
        reconciliation: dict[str, Any] | None = None
        saw_completed = False
        completed_response_output: list[Any] = []
        completed_response_id: str | None = None
        completed_turn_state: str | None = None
        active_stream: Any = None
        prepared_attempt: _PreparedOpenAiRequest | None = None
        response_committed = False
        response_id: str | None = None
        sequence_number: int | None = None
        partial_output_commit: dict[str, Any] | None = None
        preview_published = False
        generation_lineage = {"logicalCallId": logical_call_id, "generation": attempt, "partialOutputMode": delivery.mode if delivery else None} if mode == "codex" else {}
        request_start_timeout_ms = int(args.get("requestStartTimeoutMs") or OPENAI_REQUEST_START_TIMEOUT_MS)
        stream_idle_timeout_ms = int(args.get("streamIdleTimeoutMs") or OPENAI_STREAM_IDLE_TIMEOUT_MS)
        def incomplete_response(reason: str) -> Any:
            return createProviderIncompleteResponseError(
                provider="openai",
                providerLabel=provider_label,
                mode=mode,
                phase=phase,
                expectedTerminal="response.completed after every output item completed",
                reason=reason,
                attempt=attempt,
                hadAssistantOutput=had_output,
                hadToolUse=had_tool_use,
                partialAssistantBytes=len(f"{reasoning_text}{emitted_text}".encode("utf-8")),
                partialToolArgumentBytes=sum(tool_argument_bytes_by_item.values()),
                openItems=len(open_output_items) + len(pending_by_item_id),
            )

        try:
            if delivery is not None:
                begun = await delivery.begin(attempt)
                if not begun["acknowledged"]:
                    raise sink_failure(
                        provider_label=provider_label,
                        phase=phase,
                        attempt=attempt,
                        had_output=False,
                        had_tool_use=False,
                        logical_call_id=logical_call_id,
                        delivery_mode=delivery.mode,
                        reason=str(begun["reason"]),
                        operation="begin",
                    )
            _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
            client, connection_reused = await _acquire_openai_client(args, base_url)
            emitProviderActivity(runtime, "request_started", attempt=attempt)
            responses = get_value(client, "responses")
            if not responses:
                raise RuntimeError("OpenAI Responses client is unavailable.")
            logical_params = buildResponsesCreateParams(
                {
                    "isCodexBackend": mode == "codex",
                    "request": args["request"],
                    "requestOptions": openAiPromptCacheRequestOptions(
                        mode=mode,
                        base_url=base_url,
                        config=args.get("config") or {},
                        request=args["request"],
                        request_options=args.get("requestOptions") or {},
                    ),
                }
            )
            if turn_session is not None and hasattr(
                turn_session, "prepare_responses_request"
            ):
                prepared_attempt = turn_session.prepare_responses_request(
                    logical_params,
                    connection_reused=connection_reused,
                )
            else:
                prepared_attempt = _stateless_prepared_request(
                    logical_params, connection_reused
                )
            _emit(
                runtime,
                "provider.request_started",
                {
                    "provider": "openai",
                    "provider_label": provider_label,
                    "model": args["request"]["model"],
                    "attempt": attempt,
                    **_projection_event_fields(prepared_attempt),
                    **generation_lineage,
                },
            )
            response, prepared_attempt = await _start_responses_stream(
                responses=responses,
                prepared=prepared_attempt,
                runtime=runtime,
                turn_session=turn_session,
                provider_label=provider_label,
                model=args["request"]["model"],
                attempt=attempt,
                timeout_ms=request_start_timeout_ms,
            )
            active_stream = response
            if turn_session is not None:
                turn_session.bind_stream(response)
                turn_session.mark_provider_activity("response_headers")
            completed_turn_state = _extract_turn_state(response)
            emitProviderActivity(runtime, "response_headers", attempt=attempt)
            phase = "stream_open"
            iterator = to_async_iter(response).__aiter__()
            while True:
                try:
                    _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                    event = await _race_openai_activity(
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
                emitProviderActivity(runtime, "stream_frame", attempt=attempt)
                typ = str(get_value(event, "type", "") or "")
                completed_turn_state = (
                    _extract_turn_state(event)
                    or _extract_turn_state(get_value(event, "response"))
                    or completed_turn_state
                )
                if turn_session is not None:
                    activity_kind = _responses_activity_kind(typ)
                    turn_session.mark_provider_activity(activity_kind)
                emitProviderActivity(
                    runtime,
                    _responses_activity_kind(typ),
                    attempt=attempt,
                )
                event_sequence = get_value(event, "sequence_number")
                if isinstance(event_sequence, int) and not isinstance(event_sequence, bool) and event_sequence >= 0:
                    sequence_number = event_sequence
                if runtime.get("streamProviderMessages"):
                    _emit(runtime, "provider.stream_event", {"stream_event": typ, "model": args["request"]["model"], "payload": event})
                if saw_completed:
                    raise incomplete_response(
                        f"event {typ or '<unknown>'} arrived after response.completed"
                    )
                if typ == "response.created":
                    created = get_value(event, "response", {}) or {}
                    candidate_id = str(get_value(created, "id", "") or "")
                    if candidate_id:
                        response_id = candidate_id
                    continue
                if typ == "response.output_text.delta":
                    delta = str(get_value(event, "delta", "") or "")
                    if not delta:
                        continue
                    had_output = True
                    emitted_text += delta
                    delta_item_id = (
                        str(get_value(event, "item_id", "") or "")
                        or (open_message_item or "")
                    )
                    if delta_item_id:
                        item_text[delta_item_id] = item_text.get(delta_item_id, "") + delta
                    appended = await append_openai_partial(
                        delivery, generation=attempt, content_kind="assistant",
                        delta=delta, text=emitted_text, response_id=response_id,
                        sequence_number=sequence_number, provider_label=provider_label,
                        phase=phase, had_output=had_output, had_tool_use=had_tool_use,
                        logical_call_id=logical_call_id,
                    )
                    preview_published = preview_published or bool(appended["published"])
                    if runtime.get("includePartialMessages"):
                        _emit(runtime, "message.assistant.partial", {"session_id": None, "model": args["request"]["model"], "text_delta": delta, "text": emitted_text, **({"logicalCallId": logical_call_id, "generation": attempt, "responseId": response_id, "sequenceNumber": sequence_number, "deliveryMode": delivery.mode if delivery else "append_only"} if mode == "codex" else {})})
                    _append_text_block(blocks, delta)
                    continue
                if typ in {
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                }:
                    delta = str(get_value(event, "delta", "") or "")
                    if not delta:
                        continue
                    reasoning_text += delta
                    if typ == "response.reasoning_summary_text.delta":
                        reasoning_summary_text += delta
                        projected_reasoning_text = reasoning_summary_text
                    else:
                        raw_reasoning_text += delta
                        projected_reasoning_text = raw_reasoning_text
                    appended = await append_openai_partial(
                        delivery, generation=attempt, content_kind="reasoning",
                        delta=delta, text=reasoning_text, response_id=response_id,
                        sequence_number=sequence_number, provider_label=provider_label,
                        phase=phase, had_output=had_output,
                        had_tool_use=had_provisional_tool_call,
                        logical_call_id=logical_call_id,
                    )
                    preview_published = preview_published or bool(appended["published"])
                    if runtime.get("includePartialMessages"):
                        _emit(
                            runtime,
                            "message.assistant.thinking.partial",
                            {
                                "session_id": None,
                                "model": args["request"]["model"],
                                "thinking_delta": delta,
                                "thinking": projected_reasoning_text,
                                "reasoning_kind": (
                                    "summary"
                                    if typ == "response.reasoning_summary_text.delta"
                                    else "raw_thinking"
                                ),
                                "user_visible": (
                                    typ == "response.reasoning_summary_text.delta"
                                ),
                                "provider_event_type": typ,
                                **partial_openai_event_lineage(
                                    mode=mode, delivery=delivery,
                                    logical_call_id=logical_call_id,
                                    generation=attempt, response_id=response_id,
                                    sequence_number=sequence_number,
                                ),
                            },
                        )
                    continue
                if typ == "response.output_item.added":
                    item = get_value(event, "item", {})
                    item_id = str(get_value(item, "id", "") or "")
                    if not item_id:
                        raise incomplete_response("an output item started without an id")
                    if item_id in open_output_items or item_id in completed_output_items:
                        raise incomplete_response(f"output item {item_id} started more than once")
                    open_output_items[item_id] = str(get_value(item, "type", "unknown") or "unknown")
                    if get_value(item, "type") == "message":
                        open_message_item = item_id
                    if get_value(item, "type") == "function_call":
                        had_provisional_tool_call = True
                        call_id = str(get_value(item, "call_id", "") or "")
                        if call_id:
                            item_to_call_id[item_id] = call_id
                        name = str(get_value(item, "name", "") or "")
                        if name:
                            item_to_name[item_id] = name
                    continue
                if typ == "response.function_call_arguments.done":
                    item_id = str(get_value(event, "item_id", "") or "")
                    name = str(get_value(event, "name", "") or "") or item_to_name.get(item_id, "")
                    raw = str(get_value(event, "arguments", "") or "")
                    if not item_id or not name:
                        raise incomplete_response(
                            "function-call arguments completed without an item id and name"
                        )
                    if item_id not in open_output_items:
                        raise incomplete_response(
                            f"function-call arguments completed before output item {item_id} started"
                        )
                    if item_id in pending_by_item_id:
                        raise incomplete_response(
                            f"function-call arguments for output item {item_id} completed more than once"
                        )
                    try:
                        input_value = _parse_complete_json_object(raw)
                    except Exception as exc:
                        tool_argument_bytes_by_item[item_id] = len(raw.encode("utf-8"))
                        raise incomplete_response(str(exc) or "tool arguments were malformed") from exc
                    tool_argument_bytes_by_item[item_id] = len(raw.encode("utf-8"))
                    pending_by_item_id[item_id] = {
                        "name": name,
                        "raw": raw,
                        "input": input_value,
                    }
                    had_provisional_tool_call = True
                    continue
                if typ == "response.output_item.done":
                    if _close_openai_output_item(
                        {
                            "event": event,
                            "incomplete_response": incomplete_response,
                            "open_output_items": open_output_items,
                            "completed_output_items": completed_output_items,
                            "pending_by_item_id": pending_by_item_id,
                            "item_to_call_id": item_to_call_id,
                            "item_to_name": item_to_name,
                            "tool_argument_bytes_by_item": tool_argument_bytes_by_item,
                            "emitted_tool_ids": emitted_tool_ids,
                            "item_text": item_text,
                            "completed_item_records": completed_item_records,
                            "completed_item_order": completed_item_order,
                            "blocks": blocks,
                        }
                    ):
                        had_tool_use = True
                    continue
                if typ == "response.completed":
                    phase = "response_complete"
                    terminal = await _handle_response_completed(
                        {
                            "event": event,
                            "incomplete_response": incomplete_response,
                            "open_output_items": open_output_items,
                            "pending_by_item_id": pending_by_item_id,
                            "completed_item_order": completed_item_order,
                            "completed_item_records": completed_item_records,
                            "provider_label": provider_label,
                            "phase": phase,
                            "attempt": attempt,
                            "had_output": had_output,
                            "had_tool_use": had_tool_use,
                            "logical_call_id": logical_call_id,
                            "response_id": response_id,
                            "reasoning_text": reasoning_text,
                            "completed_turn_state": completed_turn_state,
                            "usage": usage,
                            "delivery": delivery,
                            "runtime": runtime,
                        }
                    )
                    blocks = terminal["blocks"]
                    emitted_text = terminal["authoritative_text"]
                    had_output = terminal["had_output"]
                    had_tool_use = terminal["had_tool_use"]
                    stop_reason = terminal["stop_reason"]
                    response_id = terminal["response_id"]
                    completed_response_output = terminal["completed_response_output"]
                    completed_response_id = terminal["completed_response_id"]
                    completed_turn_state = terminal["completed_turn_state"]
                    reconciliation = terminal["reconciliation"]
                    partial_output_commit = terminal["partial_output_commit"]
                    response_committed = True
                    saw_completed = True
                    continue
                terminal_failure = response_terminal_failure(
                    typ, event, provider_label=provider_label, phase=phase,
                    attempt=attempt, had_output=had_output,
                    had_tool_use=had_tool_use,
                )
                if terminal_failure is not None:
                    raise terminal_failure
            if not saw_completed:
                raise incomplete_response(
                    "the transport reached EOF before response.completed"
                )
            return await _finalize_responses_success(
                args=args, mode=mode, runtime=runtime,
                turn_session=turn_session, prepared=prepared_attempt,
                active_stream=active_stream,
                completed_output=completed_response_output,
                completed_response_id=completed_response_id,
                completed_turn_state=completed_turn_state, blocks=blocks,
                usage=usage, stop_reason=stop_reason, attempt=attempt,
                last_failure=last_failure, generation_lineage=generation_lineage,
                logical_call_id=logical_call_id, response_id=response_id,
                partial_output_commit=partial_output_commit,
                reconciliation=reconciliation,
            )
        except Exception as exc:
            if turn_session is not None and active_stream is not None:
                await turn_session.release_stream(active_stream)
                active_stream = None
            if (
                turn_session is not None
                and prepared_attempt is not None
                and hasattr(turn_session, "record_responses_failure")
            ):
                await turn_session.record_responses_failure(prepared_attempt, exc)
            failure, should_retry = await recover_openai_attempt(
                error=exc,
                args=args,
                mode=mode,
                provider_label=provider_label,
                phase=phase,
                attempt=attempt,
                max_attempts=max_attempts,
                had_output=had_output,
                had_tool_use=had_tool_use,
                emitted_text=emitted_text,
                reasoning_text=reasoning_text,
                had_provisional_tool_call=had_provisional_tool_call,
                response_committed=response_committed,
                delivery=delivery,
                preview_published=preview_published,
                logical_call_id=logical_call_id,
                response_id=response_id,
                sequence_number=sequence_number,
                retry_delay=providerRetryDelayMs,
            )
            last_failure = failure
            if should_retry:
                continue
            raise failure
    raise last_failure or RuntimeError("OpenAI request failed unexpectedly.")
