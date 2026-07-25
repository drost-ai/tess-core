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
