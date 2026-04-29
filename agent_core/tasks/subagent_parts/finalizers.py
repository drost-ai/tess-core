from __future__ import annotations

from .shared import *


async def finalizeBackgroundSubagentSuccessFromEnv(session: dict[str, Any], result: dict[str, Any]) -> None:
    agent_id = os.environ.get("TESS_CHILD_AGENT_ID")
    task_id = os.environ.get("TESS_CHILD_TASK_ID")
    if not agent_id or not task_id:
        return
    status = mapStopReasonToStatus(result.get("stopReason"))
    completed_at = now_iso()
    existing = await loadChildAgent(agent_id) or {
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
    summary = (result.get("text") or "").strip()[:280] or None
    retryable = status in {"failed", "stopped", "truncated", "cancelled"}
    completed_like = status in {"completed", "failed", "truncated", "cancelled", "stopped"}
    await saveChildAgent(
        {
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
        }
    )
    await writeTaskMeta(task_id, {"type": "subagent", "task_id": task_id, "agent_id": agent_id, "session_id": session["id"], "cwd": session.get("cwd"), "startedAt": session.get("createdAt"), "status": status, "completedAt": completed_at if completed_like else None, "outputFile": taskOutputPath(task_id), "stopReason": result.get("stopReason"), "summary": summary, "resultSummary": summary, "waitingReason": mapStopReasonToWaitingReason(result.get("stopReason")), "completionReason": mapStopReasonToCompletionReason(result.get("stopReason")), "attachableSessionId": session["id"], "retryable": retryable, "stoppable": False, "owner": "daemon"})


async def finalizeBackgroundSubagentFailureFromEnv(error: Any) -> None:
    agent_id = os.environ.get("TESS_CHILD_AGENT_ID")
    task_id = os.environ.get("TESS_CHILD_TASK_ID")
    session_id = os.environ.get("TESS_CHILD_SESSION_ID")
    if not agent_id or not task_id or not session_id:
        return
    message = str(error)
    completed_at = now_iso()
    existing = await loadChildAgent(agent_id) or {
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
    await saveChildAgent({**existing, "agentId": agent_id, "sessionId": session_id, "runInBackground": True, "status": "failed", "completedAt": completed_at, "taskId": task_id, "error": message, "attachableSessionId": session_id, "owner": existing.get("owner") or "daemon", "completionReason": "worker_failed", "resultSummary": message, "latestResultAt": completed_at, "retryable": True, "stoppable": False, "recoveryState": None, "lastUpdatedAt": completed_at, "lastSummary": message})
    await writeTaskMeta(task_id, {"type": "subagent", "task_id": task_id, "agent_id": agent_id, "session_id": session_id, "status": "failed", "completedAt": completed_at, "outputFile": taskOutputPath(task_id), "error": message, "summary": message, "resultSummary": message, "completionReason": "worker_failed", "attachableSessionId": session_id, "retryable": True, "stoppable": False, "owner": "daemon"})
