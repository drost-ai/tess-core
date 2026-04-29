from __future__ import annotations

from .shared import *


async def runForegroundSubagent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    context = args["context"]
    _throw_if_subagent_cancelled(context)
    prepared = await prepareSubagent(args)
    child_record = prepared["childRecord"]
    child_session = prepared["childSession"]
    agent_id = prepared["agentId"]
    started_at = _monotonic_ms()
    _output(context, "subagent.started", _subagent_started_payload(prepared, args, False))
    await saveChildAgent({**child_record, "status": "running", "lastUpdatedAt": now_iso(), "lastSummary": "Started foreground subagent run."})

    try:
        _throw_if_subagent_cancelled(context)
        result = await _execute_foreground_subagent(args, prepared)
        status = mapStopReasonToStatus(result.get("stopReason"))
        event_summary = (result.get("text") or "").strip()[:280]
        summary = event_summary or None
        completed_at = now_iso()
        await saveChildAgent(
            {
                **child_record,
                "status": status,
                "completedAt": completed_at,
                "stopReason": result.get("stopReason"),
                "lastUpdatedAt": completed_at,
                "lastSummary": summary,
            }
        )
        _output(
            context,
            "subagent.completed" if status == "completed" else "subagent.failed",
            {
                "agent_id": agent_id,
                "session_id": child_session["id"],
                "parent_session_id": context["session"]["id"],
                "attachable_session_id": child_session["id"],
                "status": status,
                "stop_reason": result.get("stopReason"),
                "summary": event_summary,
                "duration_ms": _monotonic_ms() - started_at,
            },
        )
        return {
            "content": pretty_json(
                _without_none_values(
                    {
                        "agent_id": agent_id,
                        "session_id": child_session["id"],
                        "agent_type": child_record.get("agentType"),
                        "status": status,
                        "content": result.get("text") or "",
                        "usage": result.get("usage"),
                        "tool_count": result.get("toolCount"),
                        "duration_ms": _monotonic_ms() - started_at,
                        "stop_reason": result.get("stopReason"),
                        "worktree_path": prepared.get("worktreePath"),
                    }
                )
            ),
            "isError": status != "completed",
        }
    except Exception as exc:
        cancelled = _is_subagent_abort_error(exc, context.get("abortSignal"))
        status = "cancelled" if cancelled else "failed"
        message = str(exc) or ("Subagent cancelled" if cancelled else "Subagent failed")
        completed_at = now_iso()
        next_record = {
            **child_record,
            "status": status,
            "completedAt": completed_at,
            "error": message,
            "lastUpdatedAt": completed_at,
            "lastSummary": message,
        }
        if cancelled:
            next_record["stopReason"] = "cancelled"
        await saveChildAgent(next_record)
        _output(
            context,
            "subagent.failed",
            _without_none_values(
                {
                    "agent_id": agent_id,
                    "session_id": child_session["id"],
                    "parent_session_id": context["session"]["id"],
                    "attachable_session_id": child_session["id"],
                    "status": status,
                    "stop_reason": "cancelled" if cancelled else None,
                    "error": message,
                }
            ),
        )
        return {
            "content": pretty_json(
                _without_none_values(
                    {
                        "agent_id": agent_id,
                        "session_id": child_session["id"],
                        "status": status,
                        "error": message,
                        "stop_reason": "cancelled" if cancelled else None,
                        "worktree_path": prepared.get("worktreePath"),
                    }
                )
            ),
            "isError": True,
        }


async def _execute_foreground_subagent(args: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    context = args["context"]
    _throw_if_subagent_cancelled(context)
    callback = context.get("runSubagent")
    if callable(callback):
        result = await maybe_await(callback({**args, **prepared}, context))
        _throw_if_subagent_cancelled(context)
        if isinstance(result, dict) and "content" in result and "text" not in result:
            return {"text": result.get("content") or "", "usage": result.get("usage") or {}, "toolCount": result.get("tool_count") or result.get("toolCount") or 0, "stopReason": result.get("stopReason") or result.get("stop_reason") or ("error" if result.get("isError") else "end_turn")}
        if isinstance(result, str):
            return {"text": result, "usage": {}, "toolCount": 0, "stopReason": "end_turn"}
        return result
    get_tools = context.get("getTools")
    tools = await maybe_await(get_tools()) if callable(get_tools) else context.get("tools")
    _throw_if_subagent_cancelled(context)
    if tools is None:
        raise RuntimeError("Foreground subagent execution requires getTools, tools, or runSubagent in the runtime context.")
    from ...agent import runAgentPrompt

    runtime = {
        "interactive": context.get("interactive", False),
        "askQuestion": context.get("askQuestion", lambda *_: ""),
        "emitEvent": lambda event, data: _emit_child_event(context, event, data, prepared["agentId"], prepared["childSession"]["id"]),
        "tools": tools,
        "abortSignal": context.get("abortSignal"),
    }
    call_args: dict[str, Any] = {"session": prepared["childSession"], "config": prepared["childConfig"], "prompt": "", "runtime": runtime}
    if context.get("providerOverride"):
        call_args["providerOverride"] = context["providerOverride"]
    return await runAgentPrompt(call_args)


def _emit_child_event(context: dict[str, Any], event: str, data: dict[str, Any], agent_id: str, session_id: str) -> None:
    if event.startswith("permission."):
        _output(context, event, {**data, "agent_id": agent_id, "child_session_id": session_id})
