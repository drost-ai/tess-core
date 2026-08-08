from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

import json
from typing import Any

from .._utils import pretty_json
from .cancellation import _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .permissions import _ensure_tool_permission, _parse_permission_mode_value
from .schemas import builtin_tool
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
            configured = sorted(key for key in (context["config"].get("agents") or {}) if isinstance(key, str))
            detail = (
                "Configured types: " + ", ".join(configured)
                if configured
                else "This runtime has no agent profiles configured — retry without subagent_type."
            )
            return {"content": f"Unknown subagent type: {agent_type}. {detail}", "isError": True}
        if input.get("isolation") == "worktree":
            await _ensure_tool_permission(context, "Agent", "worktree", f"Spawn subagent with isolated worktree\n{description}")
        _throw_if_tool_cancelled(context)
        from ..subagents import startBackgroundSubagent

        # Background-only: every spawn is a daemon-supervised resident child;
        # the parent never blocks on it (settled subagent-v2 design). Results
        # arrive as child messages that wake the parent at a turn boundary.
        launched = await startBackgroundSubagent(
            {
                "context": context,
                "description": description,
                "prompt": prompt,
                "agentType": agent_type,
                "model": input.get("model") if isinstance(input.get("model"), str) else None,
                "runInBackground": True,
                "name": input.get("name") if isinstance(input.get("name"), str) else None,
                "teamName": input.get("team_name") if isinstance(input.get("team_name"), str) else None,
                "mode": _parse_permission_mode_value(input.get("mode")),
                "isolation": "worktree" if input.get("isolation") == "worktree" else None,
                "parentToolUseId": context.get("currentToolUseId"),
                "parentTurnId": context.get("parentTurnId"),
                "launchGroupId": context.get("subagentLaunchGroupId"),
            }
        )
        try:
            payload = json.loads(launched.get("content") or "{}")
        except (TypeError, ValueError):
            return launched
        if payload.get("at_capacity"):
            return launched
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return launched
        # The parent never waits on a child: the spawn returns immediately and
        # the child's result arrives as a child message that wakes the parent.
        return {
            "content": pretty_json(
                {
                    **payload,
                    "result": None,
                    "note": (
                        "Spawned. The child's result will arrive as a child message. "
                        "SendMessage(to=<agent_id>) reaches it; TaskOutput/TaskStop manage it."
                    ),
                }
            )
        }

    return builtin_tool("Agent", handler)
