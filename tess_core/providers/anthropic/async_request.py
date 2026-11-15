from __future__ import annotations

from typing import Any

from ..anthropic_async_transport import (
    AnthropicAsyncHttpConnectionOwner as _AnthropicAsyncTransport,
    AsyncAnthropicHttpResponse as _AsyncAnthropicHttpResponse,
)
from .payloads import build_anthropic_headers, resolve_anthropic_messages_url
import json
from .sse import AnthropicSseAccumulator, parse_anthropic_sse_events
from .stream_consume import _normalize_anthropic_json_response


async def send_anthropic_messages_via_async_transport(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    stream: bool,
    header_features: dict[str, Any] | None,
    transport: _AnthropicAsyncTransport,
) -> tuple[str, str]:
    url = resolve_anthropic_messages_url(config.get("anthropicBaseUrl"))
    body = json.dumps(payload).encode("utf-8")
    headers = build_anthropic_headers(config, {"stream": stream}, header_features)

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


async def send_anthropic_messages_streaming_via_async_transport(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout_ms: int,
    request: dict[str, Any],
    runtime: dict[str, Any],
    attempt: int,
    header_features: dict[str, Any] | None,
    on_activity: Any | None,
    transport: _AnthropicAsyncTransport,
) -> dict[str, Any]:
    url = resolve_anthropic_messages_url(config.get("anthropicBaseUrl"))
    body = json.dumps(payload).encode("utf-8")
    headers = build_anthropic_headers(config, {"stream": True}, header_features)

    async def consume(response: _AsyncAnthropicHttpResponse) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return _normalize_anthropic_json_response(
                json.loads((await response.read()).decode("utf-8")),
                request,
            )
        return await read_anthropic_sse_response_async(
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


async def read_anthropic_sse_response_async(
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
    for raw_event in parse_anthropic_sse_events("".join(frame_lines)):
        accumulator.process_raw_event(raw_event)



