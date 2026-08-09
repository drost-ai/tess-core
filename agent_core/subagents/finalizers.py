from __future__ import annotations

import os
from .._utils import now_iso
from ..storage import mutateChildAgent, mutateTaskMeta, taskOutputPath, writeTaskMeta
from .groups import _upsert_subagent_launch_group

_TERMINAL_CHILD_STATUSES = {"completed", "failed", "cancelled", "stopped", "truncated"}
_TERMINAL_META_STATUSES = {"completed", "failed", "terminated", "stopped", "cancelled", "truncated"}
from .reconciliation import createSubagentReconciliationForTerminalChild
from .result import evaluate_subagent_result, persist_structured_subagent_result
from ..tasks.communication import append_task_event
from .permissions import (
    mapStopReasonToCompletionReason,
    mapStopReasonToStatus,
    mapStopReasonToWaitingReason,
)
from typing import Any


async def finalizeBackgroundSubagentSuccessFromEnv(session: dict[str, Any], result: dict[str, Any]) -> None:
    agent_id = os.environ.get("TESS_CHILD_AGENT_ID")
    task_id = os.environ.get("TESS_CHILD_TASK_ID")
    if not agent_id or not task_id:
        return
    status = mapStopReasonToStatus(result.get("stopReason"))
    completed_at = now_iso()
    summary = (result.get("text") or "").strip()[:280] or None
    retryable = (
        status in {"failed", "stopped", "truncated", "cancelled"}
        and result.get("stopReason") != "no_progress"
    )
    completed_like = status in {"completed", "failed", "truncated", "cancelled", "stopped"}
    structured_fields = await _background_structured_result_fields(task_id, session, result.get("text") or "")

    def _finalize_success(current: dict[str, Any] | None) -> dict[str, Any]:
        existing = current or {
            "agentId": agent_id,
            "sessionId": session["id"],
            "parentSessionId": session.get("metadata", {}).get("parentSessionId") or "",
            "model": session.get("model"),
            "provider": session.get("provider"),
            "mode": session.get("permissionMode"),
            "runInBackground": True,
            "status": "running",
            "startedAt": session.get("createdAt"),
        }
        return {
            **existing,
            "agentId": agent_id,
            "sessionId": session["id"],
            "name": session.get("metadata", {}).get("agentName"),
            "agentType": session.get("metadata", {}).get("agentType"),
            "teamName": session.get("metadata", {}).get("teamName"),
            "model": session.get("model"),
            "provider": session.get("provider"),
            "mode": session.get("permissionMode"),
            "isolation": "worktree" if session.get("metadata", {}).get("worktreePath") else None,
            "worktreePath": session.get("metadata", {}).get("worktreePath"),
            "runInBackground": True,
            "status": status,
            "completedAt": completed_at if completed_like else None,
            "taskId": task_id,
            "stopReason": result.get("stopReason"),
            "pid": None,
            "error": result.get("text") if retryable else None,
            "attachableSessionId": session["id"],
            "owner": existing.get("owner") or "daemon",
            "waitingReason": mapStopReasonToWaitingReason(result.get("stopReason")),
            "completionReason": mapStopReasonToCompletionReason(result.get("stopReason")),
            "resultSummary": summary,
            "latestResultAt": completed_at if completed_like else None,
            "retryable": retryable,
            "stoppable": False,
            "recoveryState": None,
            "lastUpdatedAt": completed_at,
            "lastSummary": summary,
            **structured_fields,
        }

    child_record = await mutateChildAgent(agent_id, _finalize_success)
    metadata = session.get("metadata") or {}
    task_meta = {
        "type": "subagent",
        "task_id": task_id,
        "agent_id": agent_id,
        "session_id": session["id"],
        "parent_session_id": metadata.get("parentSessionId"),
        "parent_task_id": metadata.get("parentTaskId"),
        "parent_tool_use_id": metadata.get("parentToolUseId"),
        "parent_turn_id": metadata.get("parentTurnId"),
        "launch_group_id": metadata.get("launchGroupId"),
        "cwd": session.get("cwd"),
        "startedAt": session.get("createdAt"),
        "status": status,
        "completedAt": completed_at if completed_like else None,
        "outputFile": taskOutputPath(task_id),
        "stopReason": result.get("stopReason"),
        "summary": summary,
        "resultSummary": summary,
        "waitingReason": mapStopReasonToWaitingReason(result.get("stopReason")),
        "completionReason": mapStopReasonToCompletionReason(result.get("stopReason")),
        "attachableSessionId": session["id"],
        "retryable": retryable,
        "stoppable": False,
        "owner": "daemon",
        "runtimeContract": metadata.get("subagentRuntimeContract"),
        **structured_fields,
    }
    await writeTaskMeta(task_id, task_meta)
    reconciliation = await createSubagentReconciliationForTerminalChild(
        childRecord=child_record,
        taskMeta=task_meta,
        session=session,
        result=result,
        source="background_finalizer",
    )
    if reconciliation:
        await append_task_event(
            task_id,
            {
                "type": "subagent.reconciliation.created",
                "agentId": agent_id,
                "childSessionId": session["id"],
                "parentSessionId": reconciliation.get("parentSessionId"),
                "reconciliationId": reconciliation.get("id"),
                "launchGroupId": reconciliation.get("launchGroupId"),
                "terminalStatus": reconciliation.get("terminalStatus"),
            },
        )
    await _upsert_parent_launch_group(session, child_record or {}, task_meta)
    await append_task_event(
        task_id,
        {
            "type": "task.subagent.completed" if status == "completed" else "task.subagent.failed",
            "agentId": agent_id,
            "childSessionId": session["id"],
            "status": status,
            "stopReason": result.get("stopReason"),
            "structuredResultStatus": structured_fields.get("structuredResultStatus"),
        },
    )


async def finalizeBackgroundSubagentFailureFromEnv(error: Any) -> None:
    agent_id = os.environ.get("TESS_CHILD_AGENT_ID")
    task_id = os.environ.get("TESS_CHILD_TASK_ID")
    session_id = os.environ.get("TESS_CHILD_SESSION_ID")
    if not agent_id or not task_id or not session_id:
        return
    message = str(error)
    completed_at = now_iso()

    def _finalize_failure(current: dict[str, Any] | None) -> dict[str, Any]:
        if current and current.get("status") in _TERMINAL_CHILD_STATUSES:
            # A committed terminal result outranks a late failure signal: the
            # success finalizer can complete durably and THEN the runner hit
            # an error (e.g. a storage timeout in a later step) - rewriting
            # completed as failed would destroy the result.
            return current
        existing = current or {
            "agentId": agent_id,
            "sessionId": session_id,
            "parentSessionId": "",
            "provider": "anthropic",
            "model": "",
            "mode": "default",
            "runInBackground": True,
            "status": "running",
            "startedAt": completed_at,
        }
        return {**existing, "agentId": agent_id, "sessionId": session_id, "runInBackground": True, "status": "failed", "completedAt": completed_at, "taskId": task_id, "error": message, "attachableSessionId": session_id, "owner": existing.get("owner") or "daemon", "completionReason": "worker_failed", "resultSummary": message, "latestResultAt": completed_at, "retryable": True, "stoppable": False, "recoveryState": None, "lastUpdatedAt": completed_at, "lastSummary": message}

    child_record = await mutateChildAgent(agent_id, _finalize_failure)
    child_record = child_record or {}
    if child_record.get("status") in _TERMINAL_CHILD_STATUSES and child_record.get("status") != "failed":
        return
    task_meta = {
        "type": "subagent",
        "task_id": task_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "parent_session_id": child_record.get("parentSessionId"),
        "parent_task_id": child_record.get("parentTaskId"),
        "parent_tool_use_id": child_record.get("parentToolUseId"),
        "parent_turn_id": child_record.get("parentTurnId"),
        "launch_group_id": child_record.get("launchGroupId"),
        "status": "failed",
        "completedAt": completed_at,
        "outputFile": taskOutputPath(task_id),
        "error": message,
        "summary": message,
        "resultSummary": message,
        "completionReason": "worker_failed",
        "attachableSessionId": session_id,
        "retryable": True,
        "stoppable": False,
        "owner": "daemon",
    }
    await mutateTaskMeta(
        task_id,
        lambda current_meta: current_meta
        if current_meta and current_meta.get("status") in _TERMINAL_META_STATUSES
        else task_meta,
        fallback=task_meta,
    )
    reconciliation = await createSubagentReconciliationForTerminalChild(
        childRecord=child_record,
        taskMeta=task_meta,
        error=message,
        source="background_failure_finalizer",
    )
    if reconciliation:
        await append_task_event(
            task_id,
            {
                "type": "subagent.reconciliation.created",
                "agentId": agent_id,
                "childSessionId": session_id,
                "parentSessionId": reconciliation.get("parentSessionId"),
                "reconciliationId": reconciliation.get("id"),
                "launchGroupId": reconciliation.get("launchGroupId"),
                "terminalStatus": reconciliation.get("terminalStatus"),
            },
        )
    await _upsert_parent_launch_group(None, child_record, task_meta)
    await append_task_event(
        task_id,
        {"type": "task.subagent.failed", "agentId": agent_id, "childSessionId": session_id, "status": "failed", "error": message},
    )


async def _background_structured_result_fields(task_id: str, session: dict[str, Any], text: str) -> dict[str, Any]:
    contract = (session.get("metadata") or {}).get("subagentRuntimeContract")
    schema = contract.get("responseSchema") if isinstance(contract, dict) else None
    if not isinstance(schema, dict):
        return {}
    evaluation = evaluate_subagent_result(text, schema)
    path = await persist_structured_subagent_result(task_id, evaluation)
    return {
        "structuredResultStatus": evaluation.get("status"),
        "structuredResult": evaluation.get("data"),
        "structuredResultErrors": evaluation.get("errors") or [],
        "structuredResultPath": path,
    }


async def _upsert_parent_launch_group(session: dict[str, Any] | None, child_record: dict[str, Any], task_meta: dict[str, Any]) -> None:
    metadata = session.get("metadata") if isinstance(session, dict) and isinstance(session.get("metadata"), dict) else {}
    parent_session_id = metadata.get("parentSessionId") or child_record.get("parentSessionId") or task_meta.get("parent_session_id")
    launch_group_id = metadata.get("launchGroupId") or child_record.get("launchGroupId") or task_meta.get("launch_group_id")
    if not isinstance(parent_session_id, str) or not parent_session_id or not isinstance(launch_group_id, str) or not launch_group_id:
        return
    await _upsert_subagent_launch_group(
        parent_session_id,
        {
            "id": launch_group_id,
            "label": metadata.get("teamName") or child_record.get("teamName") or child_record.get("name") or child_record.get("agentType") or child_record.get("agentId"),
            "childAgentIds": [child_record.get("agentId") or task_meta.get("agent_id")],
            "taskIds": [task_meta.get("task_id")],
        },
    )
