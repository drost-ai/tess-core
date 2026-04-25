from __future__ import annotations

from typing import Any

from .tools import parseAllowedPrompts


async def runForegroundSubagent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return {"content": "Foreground subagents require an embedded runtime in this Python port.", "isError": True}


async def startBackgroundSubagent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return {"content": "Background subagents require the task supervisor daemon in this Python port.", "isError": True}


async def finalizeBackgroundSubagentSuccessFromEnv(*args: Any, **kwargs: Any) -> None:
    return None


async def finalizeBackgroundSubagentFailureFromEnv(error: Any) -> None:
    return None
