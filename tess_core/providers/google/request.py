"""Send one provider call to the Gemini Interactions API and return the runtime's response."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from ..._utils import maybe_await, to_async_iter
from ...runtime.events.emit import safe_emit as _emit
from ...runtime.execution_budget import ExecutionBudgetExceeded, budget_provider_payload
from .._core import provider_retry_delay_ms, race_provider_activity, raise_if_provider_aborted
from .._knob_reader import provider_knob
from ..activity import emit_provider_activity
from ..error_evidence import ProviderWireError
from ..errors import (
    can_retry_provider_failure,
    create_provider_failure_record,
    create_provider_incomplete_response_error,
    create_provider_request_error,
    normalize_unknown_provider_failure,
)
from .params import GOOGLE_PROVIDER_LABEL, build_interactions_create_params, resolve_google_base_url
from .stream import (
    BLOCKED_CODES,
    ERROR_CODE_STATUS,
    GENERATION_ERROR_CODES,
    GoogleIncompleteStream,
    GoogleStreamError,
    InteractionAccumulator,
)

_ABORT_POLL_INTERVAL_MS = 50


async def send_google_interactions_request(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """One call: build the request, stream the interaction, retry what may be retried.

    ``config`` must already have its credentials resolved. Returns
    ``{"content": [...], "usage": {...}, "model": ..., "stopReason": ...}``.
    """
    args = {**(args or {}), **kwargs}
    config = args.get("config") or {}
    request = args["request"]
    runtime = args.get("runtime") or {}
    turn_session = args.get("turnSession") or args.get("turn_session")
    base_url = resolve_google_base_url(args.get("baseUrl") or config.get("googleBaseUrl"))
    max_attempts = max(1, int(args.get("maxAttempts") or provider_knob("provider.google_max_attempts")))
    request_start_timeout_ms = int(args.get("requestStartTimeoutMs") or provider_knob("provider.request_start_timeout_ms"))
    stream_idle_timeout_ms = int(args.get("streamIdleTimeoutMs") or provider_knob("provider.stream_idle_timeout_ms"))
    last_failure = None
    for attempt in range(1, max_attempts + 1):
        phase = "request_start"
        state = InteractionAccumulator()
        active_stream: Any = None

        def cancelled() -> None:
            raise_if_provider_aborted(
                runtime.get("abortSignal"), provider="google", provider_label=GOOGLE_PROVIDER_LABEL, message_label="Google",
                phase=phase, attempt=attempt, had_output=state.had_output, had_tool_use=state.had_tool_use,
            )

        async def race(awaitable: Any, timeout_ms: int) -> Any:
            return await race_provider_activity(
                awaitable, provider="google", provider_label=GOOGLE_PROVIDER_LABEL, message_label="Google",
                timeout_ms=timeout_ms, phase=phase, attempt=attempt, had_output=state.had_output,
                had_tool_use=state.had_tool_use, abort_signal=runtime.get("abortSignal"),
                turn_session=turn_session, abort_poll_interval_ms=_ABORT_POLL_INTERVAL_MS,
            )

        def mark(kind: str) -> None:
            emit_provider_activity(runtime, kind, attempt=attempt)
            marker = getattr(turn_session, "mark_provider_activity", None)
            if callable(marker):
                marker(kind)

        # A credential that was never resolved is the caller's fault, not a provider failure.
        client = _create_client(args, config, runtime, base_url)
        try:
            cancelled()
            emit_provider_activity(runtime, "request_started", attempt=attempt)
            params = build_interactions_create_params(
                request, {"requestOptions": args.get("requestOptions") or (config.get("providerProfile") or {}).get("requestOptions") or {}}
            )
            wire_bytes = len(json.dumps(params["input"], separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            _emit(runtime, "provider.request_started", {
                "provider": "google", "provider_label": GOOGLE_PROVIDER_LABEL, "model": request["model"], "attempt": attempt,
                "logical_input_bytes": wire_bytes, "logical_input_items": len(params["input"]),
                "transmitted_input_bytes": wire_bytes, "transmitted_input_items": len(params["input"]),
                "input_projection": "full", "previous_response_id_present": False, "connection_reused": False,
                "transport": "sse", "chain_reset_reason": "stateless_interactions",
            })
            response = await race(maybe_await(client.interactions.create(_with_execution_budget(params))), request_start_timeout_ms)
            active_stream = response
            if turn_session is not None:
                turn_session.bind_stream(response)
            mark("response_headers")
            phase = "stream_open"
            iterator = to_async_iter(response).__aiter__()
            while True:
                try:
                    cancelled()
                    event = await race(iterator.__anext__(), stream_idle_timeout_ms)
                except StopAsyncIteration:
                    break
                phase = "stream_read"
                mark("stream_frame")
                if runtime.get("streamProviderMessages"):
                    _emit(runtime, "provider.stream_event", {"stream_event": str((event or {}).get("event_type") or "interaction.event") if isinstance(event, dict) else "interaction.event", "model": request["model"], "payload": event})
                for kind, delta in state.feed(event):
                    if kind == "text":
                        mark("output_delta")
                        if runtime.get("includePartialMessages"):
                            _emit(runtime, "message.assistant.partial", {"session_id": None, "model": request["model"], "text_delta": delta, "text": state.text})
                    elif kind == "thinking":
                        mark("reasoning")
                        if runtime.get("includePartialMessages"):
                            _emit(runtime, "message.assistant.thinking.partial", {
                                "session_id": None, "model": request["model"], "thinking_delta": delta, "thinking": state.thinking,
                                "reasoning_kind": "summary", "user_visible": False, "provider_event_type": "step.delta.thought_summary",
                            })
                    else:
                        mark("tool_boundary")
                if state.status is not None:
                    break
            stop_reason = state.finish()
            phase = "response_complete"
            if turn_session is not None:
                await turn_session.release_stream(active_stream)
            else:
                await _close(active_stream)
            active_stream = None
            if attempt > 1:
                _emit(runtime, "provider.recovered", {"provider": "google", "provider_label": GOOGLE_PROVIDER_LABEL, "model": request["model"], "attempts": attempt, "category": last_failure.record.get("category") if last_failure else None})
            return {"content": state.blocks, "usage": state.usage, "model": request["model"], "stopReason": stop_reason}
        except ExecutionBudgetExceeded:
            await _release(turn_session, active_stream)
            raise
        except BaseException as exc:
            await _release(turn_session, active_stream)
            if not isinstance(exc, Exception):
                raise
            failure = _normalize_failure(exc, state, phase, attempt)
            last_failure = failure
            if not can_retry_provider_failure(failure, max_attempts):
                raise failure from None
            delay_ms = provider_retry_delay_ms(attempt, ladder=tuple(provider_knob("provider.google_retry_delays_ms")), failure=failure)
            cancelled()
            _emit(runtime, "provider.retrying", {**failure.record, "next_attempt": attempt + 1, "delay_ms": delay_ms})
            emit_provider_activity(runtime, "retry_wait", attempt=attempt, force=True)
            await asyncio.sleep(delay_ms / 1000)
            cancelled()
    raise last_failure or RuntimeError("Google Interactions request failed unexpectedly.")


def _create_client(args: dict[str, Any], config: dict[str, Any], runtime: dict[str, Any], base_url: str) -> Any:
    from ...runtime.secrets import RuntimeSecretError

    api_key = args.get("apiKey") if args.get("apiKey") is not None else config.get("googleApiKey")
    headers = args.get("headers") if args.get("headers") is not None else config.get("customHeaders") or {}
    no_auth = ((config.get("providerProfile") or {}).get("auth") or {}).get("kind") == "none"
    if (not isinstance(api_key, str) and not (api_key is None and no_auth)) or any(not isinstance(value, str) for value in headers.values()):
        raise RuntimeSecretError("secret_store_invalid", "provider credential reached a transport before resolution")
    options = {"apiKey": api_key, "baseURL": base_url, "defaultHeaders": dict(headers)}
    factory = args.get("clientFactory") or runtime.get("googleClientFactory") or config.get("googleClientFactory")
    if factory:
        return factory(options)
    from .wire import GoogleWireClient

    return GoogleWireClient(api_key=api_key, base_url=base_url, default_headers=headers)


def _with_execution_budget(params: dict[str, Any]) -> dict[str, Any]:
    """Count the call against an active execution budget and add its notice to the system instruction."""
    budgeted = budget_provider_payload({"system": params.get("system_instruction") or ""}, "google")
    if budgeted.get("system") and budgeted["system"] != (params.get("system_instruction") or ""):
        # Rebuilt key by key so the body keeps its order.
        return {"model": params["model"], "system_instruction": budgeted["system"], **{key: value for key, value in params.items() if key not in ("model", "system_instruction")}}
    return params


def _normalize_failure(error: Exception, state: InteractionAccumulator, phase: str, attempt: int) -> Any:
    common = {
        "provider": "google", "providerLabel": GOOGLE_PROVIDER_LABEL, "phase": phase, "attempt": attempt,
        "hadAssistantOutput": state.had_output, "hadToolUse": state.had_tool_use,
    }
    if isinstance(error, GoogleIncompleteStream):
        error = create_provider_incomplete_response_error(
            **common, mode="interactions", expectedTerminal="interaction.completed after every step closed", reason=str(error),
            partialAssistantBytes=len(f"{state.thinking}{state.text}".encode("utf-8")),
            partialToolArgumentBytes=state.partial_argument_bytes, openItems=state.open_steps,
        )
    elif isinstance(error, GoogleStreamError):
        code = error.code
        if code in ERROR_CODE_STATUS:
            error = ProviderWireError(str(error), status=ERROR_CODE_STATUS[code], body=json.dumps({"error": {"code": code, "message": str(error)}}), code=code, streamEventType="error")
        else:
            blocked = code in BLOCKED_CODES
            return create_provider_request_error(create_provider_failure_record(
                **common, category="client_request" if blocked else "protocol" if code in GENERATION_ERROR_CODES else "unknown",
                retryable=code in GENERATION_ERROR_CODES, code=code, streamEventType="error",
                rawMessage=(f"Gemini withheld the response ({code}): {error}" if blocked else f"{code}: {error}"),
            ))
    return normalize_unknown_provider_failure(error=error, **common)


async def _release(turn_session: Any, stream: Any) -> None:
    if stream is None:
        return
    if turn_session is not None:
        await turn_session.release_stream(stream)
    else:
        await _close(stream)


async def _close(stream: Any) -> None:
    closer = getattr(stream, "aclose", None) or getattr(stream, "close", None)
    if callable(closer):
        try:
            await maybe_await(closer())
        except Exception:
            pass
