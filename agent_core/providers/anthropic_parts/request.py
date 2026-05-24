from __future__ import annotations

import time

from .shared import *

async def sendAnthropicMessagesRequest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    config = args["config"]
    request = args["request"]
    runtime = args.get("runtime") or {}
    max_attempts = max(1, int(args.get("maxAttempts") or 2))
    stream = bool(runtime.get("streamProviderMessages") or runtime.get("includePartialMessages"))
    timeout_ms = int(
        args.get("streamIdleTimeoutMs" if stream else "jsonResponseTimeoutMs")
        or args.get("requestStartTimeoutMs")
        or (ANTHROPIC_STREAM_IDLE_TIMEOUT_MS if stream else ANTHROPIC_JSON_RESPONSE_TIMEOUT_MS)
    )
    last_failure = None

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
            payload = buildAnthropicMessagesPayload(request, {"stream": stream, "config": config})
            if stream:
                stream_activity_state = {
                    "last_activity": time.monotonic(),
                    "had_assistant_output": False,
                    "had_tool_use": False,
                }
                response = await _race_anthropic_activity(
                    _send_anthropic_messages_streaming_async(
                        config,
                        payload,
                        timeout_ms,
                        request,
                        runtime,
                        attempt,
                        on_activity=lambda accumulator=None: _mark_anthropic_stream_activity(
                            stream_activity_state,
                            accumulator,
                        ),
                    ),
                    timeout_ms=timeout_ms,
                    attempt=attempt,
                    phase=phase,
                    hadAssistantOutput=had_assistant_output,
                    hadToolUse=had_tool_use,
                    abortSignal=runtime.get("abortSignal"),
                    activityState=stream_activity_state,
                )
            else:
                raw, content_type = await _race_anthropic_activity(
                    _send_anthropic_messages_async(config, payload, timeout_ms, stream),
                    timeout_ms=timeout_ms,
                    attempt=attempt,
                    phase=phase,
                    hadAssistantOutput=had_assistant_output,
                    hadToolUse=had_tool_use,
                    abortSignal=runtime.get("abortSignal"),
                )
                phase = "parse"
                if "text/event-stream" in content_type:
                    response = normalizeAnthropicSseResponse(raw, request, runtime, attempt=attempt)
                else:
                    response = _normalize_anthropic_json_response(json.loads(raw), request)
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
            delay_ms = providerRetryDelayMs(attempt)
            _raise_anthropic_cancelled(runtime, phase, attempt, had_assistant_output, had_tool_use)
            runtime.get("emitEvent", lambda *_: None)("provider.retrying", {**failure.record, "next_attempt": attempt + 1, "delay_ms": delay_ms})
            await asyncio.sleep(delay_ms / 1000)
            _raise_anthropic_cancelled(runtime, phase, attempt, had_assistant_output, had_tool_use)
            continue
        raise failure

    raise last_failure or RuntimeError("Anthropic request failed unexpectedly.")

def _send_anthropic_messages_sync(config: dict[str, Any], payload: dict[str, Any], timeout_ms: int, stream: bool = False) -> tuple[str, str]:
    req = Request(
        resolveAnthropicMessagesUrl(config.get("anthropicBaseUrl")),
        data=json.dumps(payload).encode("utf-8"),
        headers=buildAnthropicHeaders(config, {"stream": stream}),
        method="POST",
    )
    with urlopen(req, timeout=max(1, timeout_ms) / 1000) as response:
        raw = response.read().decode("utf-8")
        content_type = response.headers.get("content-type", "")
    return raw, content_type

def _send_anthropic_messages_async(config: dict[str, Any], payload: dict[str, Any], timeout_ms: int, stream: bool = False) -> asyncio.Future[tuple[str, str]]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[str, str]] = loop.create_future()

    def complete_result(result: tuple[str, str]) -> None:
        if not future.done():
            future.set_result(result)

    def complete_error(error: BaseException) -> None:
        if not future.done():
            future.set_exception(error)

    def worker() -> None:
        try:
            result = _send_anthropic_messages_sync(config, payload, timeout_ms, stream)
        except BaseException as exc:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(complete_error, exc)
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(complete_result, result)

    threading.Thread(target=worker, name="agent-core-anthropic-request", daemon=True).start()
    return future


def _send_anthropic_messages_streaming_async(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    request: dict[str, Any],
    runtime: dict[str, Any],
    attempt: int,
    on_activity: Any | None = None,
) -> asyncio.Future[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()

    def complete_result(result: dict[str, Any]) -> None:
        if not future.done():
            future.set_result(result)

    def complete_error(error: BaseException) -> None:
        if not future.done():
            future.set_exception(error)

    def worker() -> None:
        try:
            result = _send_anthropic_messages_streaming_sync(
                config,
                payload,
                timeout_ms,
                request,
                runtime,
                attempt,
                should_stop=lambda: future.cancelled(),
                on_activity=on_activity,
            )
        except BaseException as exc:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(complete_error, exc)
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(complete_result, result)

    threading.Thread(target=worker, name="agent-core-anthropic-stream", daemon=True).start()
    return future


def _send_anthropic_messages_streaming_sync(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    request: dict[str, Any],
    runtime: dict[str, Any],
    attempt: int,
    should_stop: Any | None = None,
    on_activity: Any | None = None,
) -> dict[str, Any]:
    req = Request(
        resolveAnthropicMessagesUrl(config.get("anthropicBaseUrl")),
        data=json.dumps(payload).encode("utf-8"),
        headers=buildAnthropicHeaders(config, {"stream": True}),
        method="POST",
    )
    with urlopen(req, timeout=max(1, timeout_ms) / 1000) as response:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            raw = response.read().decode("utf-8")
            return _normalize_anthropic_json_response(json.loads(raw), request)
        return _read_anthropic_sse_response_live(
            response,
            request=request,
            runtime=runtime,
            attempt=attempt,
            should_stop=should_stop,
            on_activity=on_activity,
        )


def _read_anthropic_sse_response_live(
    response: Any,
    *,
    request: dict[str, Any],
    runtime: dict[str, Any],
    attempt: int,
    should_stop: Any | None = None,
    on_activity: Any | None = None,
) -> dict[str, Any]:
    accumulator = AnthropicSseAccumulator(request, runtime, attempt)
    frame_lines: list[str] = []
    while True:
        if callable(should_stop) and should_stop():
            break
        line = response.readline()
        if line == b"":
            break
        _mark_anthropic_stream_activity(on_activity, accumulator)
        text = line.decode("utf-8", errors="replace")
        if text.strip() == "":
            _process_anthropic_sse_frame(frame_lines, accumulator)
            _mark_anthropic_stream_activity(on_activity, accumulator)
            frame_lines = []
            continue
        frame_lines.append(text)
    _process_anthropic_sse_frame(frame_lines, accumulator)
    _mark_anthropic_stream_activity(on_activity, accumulator)
    return accumulator.finalize()


def _mark_anthropic_stream_activity(target: Any, accumulator: Any | None = None) -> None:
    if callable(target):
        target(accumulator)
        return
    if not isinstance(target, dict):
        return
    target["last_activity"] = time.monotonic()
    if accumulator is not None:
        target["had_assistant_output"] = bool(getattr(accumulator, "had_assistant_output", False))
        target["had_tool_use"] = bool(getattr(accumulator, "had_tool_use", False))


def _process_anthropic_sse_frame(
    frame_lines: list[str],
    accumulator: Any,
) -> None:
    if not frame_lines:
        return
    frame = "".join(frame_lines)
    for raw_event in parseAnthropicSseEvents(frame):
        accumulator.process_raw_event(raw_event)


def _normalize_anthropic_json_response(parsed: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": normalizeProviderBlocks(parsed.get("content", [])),
        "usage": {
            "input_tokens": (parsed.get("usage") or {}).get("input_tokens"),
            "output_tokens": (parsed.get("usage") or {}).get("output_tokens"),
            "cache_creation_input_tokens": (parsed.get("usage") or {}).get("cache_creation_input_tokens"),
            "cache_read_input_tokens": (parsed.get("usage") or {}).get("cache_read_input_tokens"),
        },
        "model": parsed.get("model") or request["model"],
        "stopReason": parsed.get("stop_reason"),
    }
