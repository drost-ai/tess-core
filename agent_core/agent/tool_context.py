from __future__ import annotations

from typing import Any, Awaitable, Callable

from ..contracts.context import ToolContext


def build_tool_context(
    *,
    session: dict[str, Any],
    config: dict[str, Any],
    runtime: dict[str, Any],
    persist: Callable[[], Awaitable[None]],
    imageBudget: Any,
    priority: int | None,
    toolUse: dict[str, Any] | None,
    parentTurnId: str,
) -> ToolContext:
    return {
        "cwd": session["cwd"],
        "allowedRoots": config.get("allowedRoots", []),
        "config": config,
        "session": session,
        "interactive": runtime.get("interactive", False),
        "outputJson": runtime.get("emitEvent", lambda *_: None),
        "askQuestion": runtime.get("askQuestion", lambda *_: ""),
        "persist_session": persist,
        "updateCwd": lambda next_cwd: session.__setitem__("cwd", next_cwd),
        "getTools": lambda: runtime.get("tools", []),
        "daemon": runtime.get("daemon"),
        "abortSignal": runtime.get("abortSignal"),
        "reserveImageAttachment": lambda image: imageBudget.reserve(image, priority),
        "currentToolUseId": toolUse.get("id") if isinstance(toolUse, dict) else None,
        "parentTurnId": parentTurnId,
        "subagentLaunchGroupId": parentTurnId,
    }
