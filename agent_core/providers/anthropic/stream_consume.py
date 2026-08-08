"""Shared consumption helpers for Anthropic responses (activity + JSON shape)."""
from __future__ import annotations

from typing import Any

import asyncio
import contextlib
import time

from .sse import normalizeProviderBlocks
from .transport import _apply_usage_snapshot


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
