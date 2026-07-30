"""Stable tool-boundary handoff support for embedding runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any


async def run_stable_boundary_hook(
    hook: Callable[[], Any] | None,
) -> dict[str, Any] | None:
    """Return a normalized stop directive after a durably persisted tool batch."""

    if not callable(hook):
        return None
    directive = hook()
    if inspect.isawaitable(directive):
        directive = await directive
    if directive is None:
        return None
    if isinstance(directive, bool):
        return {"stop": directive, "text": ""}
    if isinstance(directive, Mapping):
        stop = bool(directive.get("stop"))
        text = str(directive.get("text") or "")
    else:
        stop = bool(getattr(directive, "stop", False))
        text = str(getattr(directive, "text", "") or "")
    return {"stop": stop, "text": text}


async def finish_stable_boundary(
    *,
    hook: Callable[[], Any] | None,
    text_parts: list[str],
    tool_count: int,
    usage: dict[str, Any],
    model: str,
    estimated_cost_usd: float,
    session_id: str,
    emit: Callable[[str, dict[str, Any]], Any],
) -> dict[str, Any] | None:
    directive = await run_stable_boundary_hook(hook)
    if not directive or not directive["stop"]:
        return None
    boundary_text = directive["text"].strip()
    if boundary_text and (not text_parts or text_parts[-1] != boundary_text):
        text_parts.append(boundary_text)
    emit(
        "turn.stable_boundary_stopped",
        {"session_id": session_id, "model": model, "tool_count": tool_count},
    )
    return {
        "text": "\n\n".join(text_parts).strip(),
        "toolCount": tool_count,
        "usage": usage,
        "model": model,
        "estimatedCostUsd": estimated_cost_usd,
        "stopReason": "stable_boundary",
    }
