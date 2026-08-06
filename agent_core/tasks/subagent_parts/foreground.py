from __future__ import annotations

from .shared import *
from ...subagents.result_payload import (
    boundedSubagentError as _bounded_subagent_error,
    boundedSubagentEventSummary as _bounded_subagent_event_summary,
    boundedSubagentRuntimeContract as _bounded_subagent_runtime_contract,
    shapeForegroundSubagentResult as _shape_foreground_subagent_result,
)


async def runForegroundSubagent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    context = args["context"]
    _throw_if_subagent_cancelled(context)
    prepared = await prepareSubagent(args)
    child_record = prepared["childRecord"]
    child_session = prepared["childSession"]
    agent_id = prepared["agentId"]
    started_at = _monotonic_ms()
    bounded_runtime_contract = _bounded_subagent_runtime_contract(
        prepared.get("runtimeContractSummary")
    )
    _output(context, "subagent.started", _subagent_started_payload(prepared, args, False))
    await saveChildAgent({**child_record, "status": "running", "lastUpdatedAt": now_iso(), "lastSummary": "Started foreground subagent run."})

    try:
        _throw_if_subagent_cancelled(context)
        result = await _execute_foreground_subagent(args, prepared)
        status = mapStopReasonToStatus(result.get("stopReason"))
        event_summary = _bounded_subagent_event_summary(result.get("text") or "")
        summary = event_summary or None
        completed_at = now_iso()
        structured_fields = _foreground_structured_result_fields(prepared, result.get("text") or "")
        duration_ms = _monotonic_ms() - started_at
        parent_payload = _shape_foreground_subagent_result(
            config=context["config"],
            agentId=agent_id,
            sessionId=child_session["id"],
            agentType=child_record.get("agentType"),
            status=status,
            text=result.get("text") or "",
            usage=result.get("usage"),
            toolCount=result.get("toolCount") or 0,
            durationMs=duration_ms,
            stopReason=result.get("stopReason"),
            worktreePath=prepared.get("worktreePath"),
            structuredResultStatus=structured_fields.get("structuredResultStatus"),
            structuredResult=structured_fields.get("structuredResult"),
            structuredResultErrors=structured_fields.get("structuredResultErrors"),
            effectiveAuthority=child_record.get("effectiveAgentAuthority"),
            runtimeContract=bounded_runtime_contract,
        )
        await saveChildAgent(
            {
                **child_record,
                "status": status,
                "completedAt": completed_at,
                "stopReason": result.get("stopReason"),
                "lastUpdatedAt": completed_at,
                "lastSummary": summary,
                **structured_fields,
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
                "duration_ms": duration_ms,
                "runtime_contract": parent_payload.get("runtime_contract"),
                "structured_result_status": parent_payload.get("structured_result_status"),
                "structured_result": parent_payload.get("structured_result"),
                "structured_result_truncated": parent_payload.get("structured_result_truncated"),
                "structured_result_errors": parent_payload.get("structured_result_errors"),
                "recovery": parent_payload["recovery"],
            },
        )
        return {
            "content": pretty_json(parent_payload),
            "isError": status != "completed",
        }
    except Exception as exc:
        cancelled = _is_subagent_abort_error(exc, context.get("abortSignal"))
        status = "cancelled" if cancelled else "failed"
        message = _bounded_subagent_error(exc)
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
                        "runtime_contract": bounded_runtime_contract,
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

    from ...agent_runtime_bindings import createAgentRuntimeBindings, isTaskSupervisorGateway
    from ...runtime_generation import createRuntimeSessionBinding

    daemon = context.get("daemon")
    runtime = createAgentRuntimeBindings({
        "emitEvent": lambda event, data: _emit_child_event(context, event, data, prepared["agentId"], prepared["childSession"]["id"]),
        "tools": tools,
        "daemon": {"state": "connected", "client": daemon} if isTaskSupervisorGateway(daemon) else {"state": "unavailable", "reason": context.get("daemonUnavailableReason") or {"code": "subagent.daemon_unavailable", "message": "The task daemon is unavailable to this foreground subagent."}},
        "refreshMcpToolRegistry": context.get("refreshMcpToolRegistry") or (lambda: None),
        "sessionBinding": createRuntimeSessionBinding(prepared["childSession"]["id"], 1, 1),
        "questionPolicy": {"interactive": context.get("interactive", False), "askQuestion": context.get("askQuestion", lambda *_: "")},
        "abortSignal": context.get("abortSignal"),
    })
    call_args: dict[str, Any] = {"session": prepared["childSession"], "config": prepared["childConfig"], "prompt": "", "runtime": runtime}
    if context.get("providerOverride"):
        call_args["providerOverride"] = context["providerOverride"]
    return await runAgentPrompt(call_args)


def _emit_child_event(context: dict[str, Any], event: str, data: dict[str, Any], agent_id: str, session_id: str) -> None:
    if event.startswith("permission."):
        _output(context, event, {**data, "agent_id": agent_id, "child_session_id": session_id})


def _foreground_structured_result_fields(prepared: dict[str, Any], text: str) -> dict[str, Any]:
    contract = prepared.get("runtimeContract")
    schema = contract.get("responseSchema") if isinstance(contract, dict) else None
    if not isinstance(schema, dict):
        return {}
    evaluation = evaluate_subagent_result(text, schema)
    return {
        "structuredResultStatus": evaluation.get("status"),
        "structuredResult": evaluation.get("data"),
        "structuredResultErrors": evaluation.get("errors") or [],
    }
