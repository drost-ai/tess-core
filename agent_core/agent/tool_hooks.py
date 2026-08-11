from __future__ import annotations

from typing import Any

from ..authority.hooks import fire_tool_hooks


def build_tool_hook_context(
    session: dict[str, Any], config: dict[str, Any], authority: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "cwd": session["cwd"],
        "sessionId": session["id"],
        "config": config,
        "permissionMode": session.get("permissionMode") or config.get("permissionMode"),
        "authority": authority,
    }


async def run_pre_tool_hook(
    tool: Any, tool_use: dict[str, Any], fire: dict[str, Any], emit: Any
) -> dict[str, Any]:
    try:
        return await fire_tool_hooks(
            {
                "event": "PreToolUse",
                "toolName": tool.name,
                "toolInput": tool_use.get("input") or {},
                "fire": fire,
                "emit": emit,
            }
        )
    except Exception:
        return {"decision": "continue"}


async def run_post_tool_hook(
    tool: Any, tool_use: dict[str, Any], fire: dict[str, Any], emit: Any
) -> None:
    try:
        await fire_tool_hooks(
            {
                "event": "PostToolUse",
                "toolName": tool.name,
                "toolInput": tool_use.get("input") or {},
                "fire": fire,
                "emit": emit,
            }
        )
    except Exception:
        pass
