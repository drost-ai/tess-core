"""Streaming response consumption for the Anthropic SSE wire format."""
from __future__ import annotations

from typing import Any

import asyncio
import contextlib
import json
import time

from .sse import AnthropicSseAccumulator, normalizeProviderBlocks, parseAnthropicSseEvents
from .transport import _apply_usage_snapshot

def _consume_anthropic_stream_response(
    response: Any,
    *,
    request: dict[str, Any],
    runtime: dict[str, Any],
    attempt: int,
    should_stop: Any | None,
    on_activity: Any | None,
) -> dict[str, Any]:
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
    target["saw_activity"] = True
    if accumulator is not None:
        target["had_assistant_output"] = bool(getattr(accumulator, "had_assistant_output", False))
        target["had_tool_use"] = bool(getattr(accumulator, "had_tool_use", False))


def _record_anthropic_turn_activity(
    target: dict[str, Any],
    accumulator: Any | None,
) -> None:
    _mark_anthropic_stream_activity(target, accumulator)
    event = target.get("activity_event")
    loop = target.get("activity_loop")
    if isinstance(event, asyncio.Event) and loop is not None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)


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
    usage: dict[str, Any] = {}
    _apply_usage_snapshot(usage, parsed.get("usage") or {})
    response = {
        "content": normalizeProviderBlocks(parsed.get("content", [])),
        "usage": usage,
        "model": parsed.get("model") or request["model"],
        "stopReason": parsed.get("stop_reason"),
    }
    response["_anthropicResponseId"] = parsed.get("id") if isinstance(parsed.get("id"), str) else None
    response["_anthropicDiagnosticsFieldPresent"] = "diagnostics" in parsed
    response["_anthropicDiagnostics"] = parsed.get("diagnostics")
    return response
