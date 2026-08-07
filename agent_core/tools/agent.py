from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

import asyncio
import json
from typing import Any

from .._utils import pretty_json
from .cancellation import _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .permissions import _ensure_tool_permission, _parse_permission_mode_value
from .schemas import builtin_tool
from .validation import _require_string
from ..subagents.depth_policy import checkSubagentDepthAllowed
from ..tasks.communication import mark_task_message_delivered, read_task_mailbox_messages


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
        from ..subagents import startBackgroundSubagent

        # Background-only: every spawn is a daemon-supervised resident child.
        # The call still blocks for the child's initial result so the
        # model-facing contract keeps its ask-and-receive feel.
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
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return launched
        # Call-time import: config package imports tools at module load.
        from ..config._knobs import knobValue

        timeout_ms = int(knobValue("subagents.initial_result_timeout_ms"))
        initial = await _await_initial_result(task_id, timeout_ms, context)
        if initial is None:
            return {
                "content": pretty_json(
                    {
                        **payload,
                        "result": None,
                        "note": (
                            f"The child has not reported within {timeout_ms}ms. It stays resident; "
                            "its result will arrive as a child message, and SendMessage(to=<agent_id>) reaches it."
                        ),
                    }
                )
            }
        return {
            "content": pretty_json(
                {
                    **payload,
                    "status": "idle_resident",
                    "result": initial.get("content"),
                    "note": "The child stays resident: SendMessage(to=<agent_id>) sends follow-up work in the same context.",
                }
            )
        }

    return builtin_tool("Agent", handler)


async def _await_initial_result(task_id: str, timeout_ms: int, context: ToolContext) -> dict[str, Any] | None:
    from ..config._knobs import knobValue

    poll_seconds = int(knobValue("comms.reply_poll_ms")) / 1000
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        signal = context.get("abortSignal")
        if signal is not None and getattr(signal, "aborted", False):
            return None
        for record in await read_task_mailbox_messages(task_id):
            if record.get("direction") != "to_parent" or record.get("kind") != "result":
                continue
            if record.get("status") == "queued":
                # Claim it: the result rides this tool result, so the boundary
                # drain must not re-inject it as a child message.
                await mark_task_message_delivered(task_id, str(record.get("messageId")))
            return record
        await asyncio.sleep(poll_seconds)
    return None
