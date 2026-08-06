from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

from typing import Any

from .cancellation import _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .permissions import _ensure_tool_permission, _parse_permission_mode_value
from .schemas import _tool_description, _tool_schema
from .validation import _require_string
from ..subagents.depth_policy import checkSubagentDepthAllowed


def _agent_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        depth = checkSubagentDepthAllowed(context["session"], context.get("config"))
        if not depth["allowed"]:
            output = context.get("outputJson")
            if callable(output):
                output("subagent.depth_blocked", depth)
            return {"content": depth["message"], "isError": True}
        _throw_if_tool_cancelled(context)
        description = _require_string(input.get("description"), "description")
        prompt = _require_string(input.get("prompt"), "prompt")
        agent_type = input.get("subagent_type").strip() if isinstance(input.get("subagent_type"), str) else None
        if agent_type and agent_type not in context["config"].get("agents", {}):
            return {"content": f"Unknown subagent type: {agent_type}", "isError": True}
        if input.get("isolation") == "worktree":
            await _ensure_tool_permission(context, "Agent", "worktree", f"Spawn subagent with isolated worktree\n{description}")
        _throw_if_tool_cancelled(context)
        from ..subagents import runForegroundSubagent, startBackgroundSubagent

        agent_args = {
            "context": context,
            "description": description,
            "prompt": prompt,
            "agentType": agent_type,
            "model": input.get("model") if isinstance(input.get("model"), str) else None,
            "runInBackground": bool(input.get("run_in_background")),
            "name": input.get("name") if isinstance(input.get("name"), str) else None,
            "teamName": input.get("team_name") if isinstance(input.get("team_name"), str) else None,
            "mode": _parse_permission_mode_value(input.get("mode")),
            "isolation": "worktree" if input.get("isolation") == "worktree" else None,
            "parentToolUseId": context.get("currentToolUseId"),
            "parentTurnId": context.get("parentTurnId"),
            "launchGroupId": context.get("subagentLaunchGroupId"),
        }
        if input.get("run_in_background"):
            return await startBackgroundSubagent(agent_args)
        return await runForegroundSubagent(agent_args)

    return ToolDefinition("Agent", _tool_description("Agent"), _tool_schema("Agent"), handler)
