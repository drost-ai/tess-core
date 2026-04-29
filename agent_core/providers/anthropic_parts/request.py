from __future__ import annotations

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
        try:
            _raise_anthropic_cancelled(runtime, phase, attempt, had_assistant_output, had_tool_use)
            runtime.get("emitEvent", lambda *_: None)(
                "provider.request_started",
                {"provider": "anthropic", "provider_label": "Anthropic", "model": request["model"], "attempt": attempt},
            )
            payload = buildAnthropicMessagesPayload(request, {"stream": stream})
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
                parsed = json.loads(raw)
                response = {
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
                    "hadAssistantOutput": had_assistant_output,
                    "hadToolUse": had_tool_use,
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
                    "hadAssistantOutput": had_assistant_output,
                    "hadToolUse": had_tool_use,
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
