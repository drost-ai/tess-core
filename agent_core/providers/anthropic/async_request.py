from __future__ import annotations

from typing import Any

from ..anthropic_async_transport import (
    AnthropicAsyncHttpConnectionOwner as _AnthropicHttpConnectionOwner,
    AsyncAnthropicHttpResponse as _AsyncAnthropicHttpResponse,
)
from .payloads import buildAnthropicHeaders, resolveAnthropicMessagesUrl
from .shared import json
from .sse import AnthropicSseAccumulator, normalizeProviderBlocks, parseAnthropicSseEvents
from .transport import _apply_usage_snapshot


async def sendAnthropicMessagesViaAsyncTransport(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    stream: bool,
    header_features: dict[str, Any] | None,
    transport: _AnthropicHttpConnectionOwner,
) -> tuple[str, str]:
    url = resolveAnthropicMessagesUrl(config.get("anthropicBaseUrl"))
    body = json.dumps(payload).encode("utf-8")
    headers = buildAnthropicHeaders(config, {"stream": stream}, header_features)

    async def consume(response: _AsyncAnthropicHttpResponse) -> tuple[str, str]:
        return (
            (await response.read()).decode("utf-8"),
            response.headers.get("content-type", ""),
        )

    return await transport.request(
        url=url,
        body=body,
        headers=headers,
        timeout_seconds=max(1, timeout_ms) / 1000,
        consume=consume,
    )


async def sendAnthropicMessagesStreamingViaAsyncTransport(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    request: dict[str, Any],
    runtime: dict[str, Any],
    attempt: int,
    header_features: dict[str, Any] | None,
    on_activity: Any | None,
    transport: _AnthropicHttpConnectionOwner,
) -> dict[str, Any]:
    url = resolveAnthropicMessagesUrl(config.get("anthropicBaseUrl"))
    body = json.dumps(payload).encode("utf-8")
    headers = buildAnthropicHeaders(config, {"stream": True}, header_features)

    async def consume(response: _AsyncAnthropicHttpResponse) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return _normalize_anthropic_json_response(
                json.loads((await response.read()).decode("utf-8")),
                request,
            )
        return await readAnthropicSseResponseAsync(
            response,
            request=request,
            runtime=runtime,
            attempt=attempt,
            on_activity=on_activity,
        )

    return await transport.request(
        url=url,
        body=body,
        headers=headers,
        timeout_seconds=max(1, timeout_ms) / 1000,
        consume=consume,
    )


async def readAnthropicSseResponseAsync(
    response: _AsyncAnthropicHttpResponse,
    *,
    request: dict[str, Any],
    runtime: dict[str, Any],
    attempt: int,
    on_activity: Any | None,
) -> dict[str, Any]:
    accumulator = AnthropicSseAccumulator(request, runtime, attempt)
    frame_lines: list[str] = []
    while True:
        line = await response.readline()
        if line == b"":
            break
        _mark_activity(on_activity, accumulator)
        text = line.decode("utf-8", errors="replace")
        if text.strip() == "":
            _process_frame(frame_lines, accumulator)
            _mark_activity(on_activity, accumulator)
            frame_lines = []
            continue
        frame_lines.append(text)
    _process_frame(frame_lines, accumulator)
    _mark_activity(on_activity, accumulator)
    return accumulator.finalize()


def _mark_activity(target: Any, accumulator: Any) -> None:
    if callable(target):
        target(accumulator)


def _process_frame(frame_lines: list[str], accumulator: Any) -> None:
    if not frame_lines:
        return
    for raw_event in parseAnthropicSseEvents("".join(frame_lines)):
        accumulator.process_raw_event(raw_event)


def _normalize_anthropic_json_response(
    parsed: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    _apply_usage_snapshot(usage, parsed.get("usage") or {})
    response = {
        "content": normalizeProviderBlocks(parsed.get("content", [])),
        "usage": usage,
        "model": parsed.get("model") or request["model"],
        "stopReason": parsed.get("stop_reason"),
    }
    response["_anthropicResponseId"] = (
        parsed.get("id") if isinstance(parsed.get("id"), str) else None
    )
    response["_anthropicDiagnosticsFieldPresent"] = "diagnostics" in parsed
    response["_anthropicDiagnostics"] = parsed.get("diagnostics")
    return response


send_anthropic_messages_via_async_transport = sendAnthropicMessagesViaAsyncTransport
send_anthropic_messages_streaming_via_async_transport = sendAnthropicMessagesStreamingViaAsyncTransport
