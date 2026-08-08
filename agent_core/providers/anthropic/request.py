from __future__ import annotations

from .._knob_reader import provider_knob

import copy
import time

from ...contracts.providers import ProviderResponse
from ..activity import emitProviderActivity
from ...runtime.secret_provider import resolveProviderConfigSecrets
from ..anthropic_async_transport import (
    AnthropicAsyncHttpConnectionOwner as _AnthropicAsyncTransport,
)
from ..anthropic_http_transport import startAnthropicTransportWorker as _start_anthropic_transport_worker

import asyncio
import contextlib
import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..errors import (
    canRetryProviderFailure,
    classifyProviderHttpFailure,
    normalizeUnknownProviderFailure,
    providerRetryDelayMs,
)
from ..prompt_cache_diagnostics import normalizePromptCacheProviderReason
from .payloads import (
    buildAnthropicHeaders,
    buildAnthropicMessagesEnvelope,
    resolveAnthropicMessagesUrl,
)
from .stream_consume import (
    _consume_anthropic_stream_response,
    _normalize_anthropic_json_response,
    _record_anthropic_turn_activity,
)
from .sse import _safe_json_parse, normalizeAnthropicSseResponse
from .transport import (
    _activity_state_bool,
    _race_anthropic_activity,
    _raise_anthropic_cancelled,
)
from .async_request import (
    sendAnthropicMessagesStreamingViaAsyncTransport as _send_anthropic_messages_streaming_via_async_transport,
    sendAnthropicMessagesViaAsyncTransport as _send_anthropic_messages_via_async_transport,
)
_DEFAULT_URLOPEN = urlopen

async def sendAnthropicMessagesRequest(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderResponse:
    # Call-time import: the config package imports providers at module load.
    from ...config._knobs import knobValue

    args = {**(args or {}), **kwargs}
    config = await resolveProviderConfigSecrets(args["config"])
    request = args["request"]
    runtime = args.get("runtime") or {}
    max_attempts = max(1, int(args.get("maxAttempts") or provider_knob("provider.anthropic_max_attempts")))
    stream = bool(runtime.get("streamProviderMessages") or runtime.get("includePartialMessages"))
    timeout_ms = int(
        args.get("streamIdleTimeoutMs" if stream else "jsonResponseTimeoutMs")
        or args.get("requestStartTimeoutMs")
        or knobValue("provider.stream_idle_timeout_ms" if stream else "provider.json_response_timeout_ms")
    )
    envelope = buildAnthropicMessagesEnvelope(request, config, {"stream": stream})
    last_failure = None
    turn_session = args.get("turnSession") or args.get("turn_session")

    for attempt in range(1, max_attempts + 1):
        phase = "request_start"
        had_assistant_output = False
        had_tool_use = False
        stream_activity_state: dict[str, Any] | None = None
        try:
            _raise_anthropic_cancelled(runtime, phase, attempt, had_assistant_output, had_tool_use)
            runtime.get("emitEvent", lambda *_: None)(
                "provider.request_started",
                {"provider": "anthropic", "provider_label": "Anthropic", "model": request["model"], "attempt": attempt},
            )
            emitProviderActivity(runtime, "request_started", attempt=attempt)
            payload = envelope["payload"]
            if stream:
                activity_loop = asyncio.get_running_loop()
                stream_activity_state = {
                    "last_activity": time.monotonic(),
                    "saw_activity": False,
                    "had_assistant_output": False,
                    "had_tool_use": False,
                    "activity_event": asyncio.Event(),
                    "activity_loop": activity_loop,
                }
                stream_sender = _send_anthropic_messages_streaming_async(
                    config,
                    payload,
                    timeout_ms,
                    request,
                    runtime,
                    attempt,
                    envelope.get("headerFeatures"),
                    on_activity=lambda accumulator=None: _record_anthropic_turn_activity(
                        stream_activity_state,
                        accumulator,
                    ),
                    turn_session=turn_session,
                )
                response = await _race_anthropic_activity(
                    stream_sender,
                    timeout_ms=timeout_ms,
                    attempt=attempt,
                    phase=phase,
                    hadAssistantOutput=had_assistant_output,
                    hadToolUse=had_tool_use,
                    abortSignal=runtime.get("abortSignal"),
                    activityState=stream_activity_state,
                    turnSession=turn_session,
                )
            else:
                sender = _send_anthropic_messages_async(
                    config, payload, timeout_ms, stream, envelope.get("headerFeatures"), turn_session=turn_session
                )
                raw, content_type = await _race_anthropic_activity(
                    sender,
                    timeout_ms=timeout_ms,
                    attempt=attempt,
                    phase=phase,
                    hadAssistantOutput=had_assistant_output,
                    hadToolUse=had_tool_use,
                    abortSignal=runtime.get("abortSignal"),
                    turnSession=turn_session,
                )
                emitProviderActivity(runtime, "response_headers", attempt=attempt)
                phase = "parse"
                if "text/event-stream" in content_type:
                    response = normalizeAnthropicSseResponse(
                        raw,
                        request,
                        {**runtime, "_capturePromptCacheMetadata": True},
                        attempt=attempt,
                    )
                else:
                    response = _normalize_anthropic_json_response(json.loads(raw), request)
            response = _attach_prompt_cache_observation(response, envelope)
            if attempt > 1:
                runtime.get("emitEvent", lambda *_: None)(
                    "provider.recovered",
                    {
                        "provider": "anthropic",
                        "provider_label": "Anthropic",
                        "model": request["model"],
                        "attempts": attempt,
                        "category": (last_failure.record if last_failure else {}).get("category"),
                    },
                )
            return response
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            with contextlib.suppress(Exception):
                exc.close()
            parsed = _safe_json_parse(body) or {}
            error_payload = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            error_type = error_payload.get("type") if isinstance(error_payload.get("type"), str) else parsed.get("type") if isinstance(parsed.get("type"), str) else None
            failure = classifyProviderHttpFailure(
                {
                    "provider": "anthropic",
                    "providerLabel": "Anthropic",
                    "phase": phase,
                    "status": exc.code,
                    "attempt": attempt,
                    "hadAssistantOutput": _activity_state_bool(stream_activity_state, "had_assistant_output", had_assistant_output),
                    "hadToolUse": _activity_state_bool(stream_activity_state, "had_tool_use", had_tool_use),
                    "body": body,
                    "errorType": error_type,
                }
            )
        except Exception as exc:
            failure = normalizeUnknownProviderFailure(
                {
                    "error": exc,
                    "provider": "anthropic",
                    "providerLabel": "Anthropic",
                    "phase": phase,
                    "attempt": attempt,
                    "hadAssistantOutput": _activity_state_bool(stream_activity_state, "had_assistant_output", had_assistant_output),
                    "hadToolUse": _activity_state_bool(stream_activity_state, "had_tool_use", had_tool_use),
                }
            )
        last_failure = failure
        if canRetryProviderFailure(failure, max_attempts):
            delay_ms = providerRetryDelayMs(attempt, failure)
            _raise_anthropic_cancelled(runtime, phase, attempt, had_assistant_output, had_tool_use)
            runtime.get("emitEvent", lambda *_: None)("provider.retrying", {**failure.record, "next_attempt": attempt + 1, "delay_ms": delay_ms})
            emitProviderActivity(
                runtime,
                "retry_wait",
                attempt=attempt,
                force=True,
            )
            await asyncio.sleep(delay_ms / 1000)
            _raise_anthropic_cancelled(runtime, phase, attempt, had_assistant_output, had_tool_use)
            continue
        raise failure

    raise last_failure or RuntimeError("Anthropic request failed unexpectedly.")
def _send_anthropic_messages_sync(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    stream: bool = False,
    header_features: dict[str, Any] | None = None,
    transport: Any = None,
) -> tuple[str, str]:
    url = resolveAnthropicMessagesUrl(config.get("anthropicBaseUrl"))
    body = json.dumps(payload).encode("utf-8")
    headers = buildAnthropicHeaders(config, {"stream": stream}, header_features)
    if transport is not None:
        def consume(response: Any) -> tuple[str, str]:
            return (
                response.read().decode("utf-8"),
                response.headers.get("content-type", ""),
            )

        return transport.request(
            url=url,
            body=body,
            headers=headers,
            timeout_seconds=max(1, timeout_ms) / 1000,
            consume=consume,
        )
    req = Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    with urlopen(req, timeout=max(1, timeout_ms) / 1000) as response:
        raw = response.read().decode("utf-8")
        content_type = response.headers.get("content-type", "")
    return raw, content_type
def _send_anthropic_messages_async(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    stream: bool = False,
    header_features: dict[str, Any] | None = None,
    turn_session: Any = None,
) -> asyncio.Future[tuple[str, str]]:
    if (
        urlopen is not _DEFAULT_URLOPEN
        or _send_anthropic_messages_sync is not _DEFAULT_SEND_ANTHROPIC_MESSAGES_SYNC
    ):
        return _start_anthropic_transport_worker(
            target=lambda: _send_anthropic_messages_sync(
                config, payload, timeout_ms, stream, header_features, None
            ),
            turn_session=turn_session,
            name="agent-core-anthropic-request-test-double",
        )
    transport = _acquire_anthropic_transport(turn_session)
    future = asyncio.create_task(
        _send_anthropic_messages_via_async_transport(
            config, payload, timeout_ms, stream, header_features, transport
        )
    )
    future._anthropic_transport_owner = transport  # type: ignore[attr-defined]
    future._anthropic_turn_session = turn_session  # type: ignore[attr-defined]
    return future
def _send_anthropic_messages_streaming_async(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    request: dict[str, Any],
    runtime: dict[str, Any],
    attempt: int,
    header_features: dict[str, Any] | None = None,
    on_activity: Any | None = None,
    turn_session: Any = None,
) -> asyncio.Future[dict[str, Any]]:
    if (
        urlopen is not _DEFAULT_URLOPEN
        or _send_anthropic_messages_streaming_sync
        is not _DEFAULT_SEND_ANTHROPIC_MESSAGES_STREAMING_SYNC
    ):
        future_holder: dict[str, asyncio.Future[dict[str, Any]]] = {}
        future = _start_anthropic_transport_worker(
            target=lambda: _send_anthropic_messages_streaming_sync(
                config,
                payload,
                timeout_ms,
                request,
                runtime,
                attempt,
                header_features=header_features,
                should_stop=lambda: bool(
                    future_holder.get("future")
                    and future_holder["future"].cancelled()
                ),
                on_activity=on_activity,
                transport=None,
            ),
            turn_session=turn_session,
            name="agent-core-anthropic-stream-test-double",
        )
        future_holder["future"] = future
        return future
    transport = _acquire_anthropic_transport(turn_session)
    future = asyncio.create_task(
        _send_anthropic_messages_streaming_via_async_transport(
            config,
            payload,
            timeout_ms,
            request,
            runtime,
            attempt,
            header_features,
            on_activity,
            transport,
        )
    )
    future._anthropic_transport_owner = transport  # type: ignore[attr-defined]
    future._anthropic_turn_session = turn_session  # type: ignore[attr-defined]
    return future


def _send_anthropic_messages_streaming_sync(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    request: dict[str, Any],
    runtime: dict[str, Any],
    attempt: int,
    header_features: dict[str, Any] | None = None,
    should_stop: Any | None = None,
    on_activity: Any | None = None,
    transport: Any = None,
) -> dict[str, Any]:
    url = resolveAnthropicMessagesUrl(config.get("anthropicBaseUrl"))
    body = json.dumps(payload).encode("utf-8")
    headers = buildAnthropicHeaders(config, {"stream": True}, header_features)
    if transport is not None:
        return transport.request(
            url=url,
            body=body,
            headers=headers,
            timeout_seconds=max(1, timeout_ms) / 1000,
            consume=lambda response: _consume_anthropic_stream_response(
                response,
                request=request,
                runtime=runtime,
                attempt=attempt,
                should_stop=should_stop,
                on_activity=on_activity,
            ),
        )
    req = Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    with urlopen(req, timeout=max(1, timeout_ms) / 1000) as response:
        return _consume_anthropic_stream_response(
            response,
            request=request,
            runtime=runtime,
            attempt=attempt,
            should_stop=should_stop,
            on_activity=on_activity,
        )


def _acquire_anthropic_transport(turn_session: Any) -> _AnthropicAsyncTransport:
    acquire = getattr(turn_session, "acquire_anthropic_transport", None)
    if callable(acquire):
        return acquire()
    return _AnthropicAsyncTransport()


# Preserve the established test-double seam while production traffic uses the
# cancellable asyncio transport above.
_DEFAULT_SEND_ANTHROPIC_MESSAGES_SYNC = _send_anthropic_messages_sync
_DEFAULT_SEND_ANTHROPIC_MESSAGES_STREAMING_SYNC = _send_anthropic_messages_streaming_sync


def _attach_prompt_cache_observation(
    response: dict[str, Any], envelope: dict[str, Any]
) -> dict[str, Any]:
    response_id = response.pop("_anthropicResponseId", None)
    field_present = bool(response.pop("_anthropicDiagnosticsFieldPresent", False))
    diagnostics = response.pop("_anthropicDiagnostics", None)
    observation = envelope.get("promptCache")
    if not isinstance(observation, dict):
        return response
    observation = copy.deepcopy(observation)
    requested = bool(envelope.get("diagnostics", {}).get("requested"))
    if requested and isinstance(response_id, str):
        observation["responseId"] = response_id
    observation["providerDiagnosis"] = normalizePromptCacheProviderReason(
        {
            "requested": requested,
            "previousMessageIdSent": bool(envelope.get("diagnostics", {}).get("previousMessageIdSent")),
            "fieldPresent": field_present,
            "diagnostics": diagnostics,
        }
    )
    response["promptCache"] = observation
    return response
