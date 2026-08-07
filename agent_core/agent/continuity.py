"""Continuity turns: resume an idle parent when children send messages.

A continuity turn is a normal agent turn with no fresh user prompt: the
loop's boundary drain injects the queued child messages as
task-notification user messages and the model reacts to them. Embedders
wire this as the wake controller's resume callback.
"""
from __future__ import annotations

from typing import Any

from .loop import runAgentPrompt


async def runParentContinuityTurn(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    values = {**(args or {}), **kwargs, "prompt": ""}
    return await runAgentPrompt(values)
