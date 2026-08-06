from __future__ import annotations

from ._shared import _string

from ..runtime.events.emit import safeEmit as _emit

import uuid
from typing import Any

from .._utils import now_iso
from ..contracts.records import launch_task_id as _launch_task_id
from ..storage import fileLock, loadSession, saveSession
from .groups import (
    _read_subagent_launch_groups,
)
from .reconciliation import (
    TERMINAL_RECONCILIATION_STATUSES,
    _read_records,
    _sort_records,
    _write_records,
    readSubagentReconciliations,
    listUnconsumedSubagentReconciliations,
    subagentReconciliationLogPath,
)




def evaluateParentContinuationReadiness(records: list[dict[str, Any]], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy or {}
    wait_for = _string(policy.get("waitFor") or policy.get("wait_for") or "manual")
    if wait_for not in {"manual", "any", "all", "quorum"}:
        wait_for = "manual"
    group = policy.get("group") if isinstance(policy.get("group"), dict) else None
    if group and group.get("continuationTaskId"):
        return {"ready": False, "reason": "already_requested", "total": len(group.get("childAgentIds") or []), "terminal": 0}
    if group:
        child_ids = [child_id for child_id in (group.get("childAgentIds") or []) if isinstance(child_id, str) and child_id]
        child_id_set = set(child_ids)
        matching = [record for record in records if record.get("childAgentId") in child_id_set or record.get("launchGroupId") == group.get("id")]
        terminal_ids = {
            record.get("childAgentId")
            for record in matching
            if record.get("terminalStatus") in TERMINAL_RECONCILIATION_STATUSES and isinstance(record.get("childAgentId"), str)
        }
        total = len(child_ids)
        terminal = len(terminal_ids)
    else:
        total = len(records)
        terminal = len([record for record in records if record.get("terminalStatus") in TERMINAL_RECONCILIATION_STATUSES])
    if wait_for == "manual":
        return {"ready": False, "reason": "manual", "total": total, "terminal": terminal}
    if wait_for == "any":
        return {"ready": terminal > 0, "reason": "any", "total": total, "terminal": terminal}
    if wait_for == "all":
        return {"ready": total > 0 and terminal >= total, "reason": "all", "total": total, "terminal": terminal}
    quorum = int(policy.get("quorumCount") or policy.get("quorum_count") or 1)
    return {"ready": terminal >= quorum, "reason": "quorum", "total": total, "terminal": terminal, "quorum": quorum}




def defaultParentContinuationPrompt() -> str:
    return "Continue from the completed background subagent results. Synthesize the child outputs, note uncertainty, and decide the next action."


async def _maybe_start_ready_parent_continuations(
    parentSessionId: str,
    *,
    launchGroupId: str | None = None,
    policy: dict[str, Any] | None = None,
    starter: Any = None,
    runtime: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    policy = policy if isinstance(policy, dict) else {}
    if not policy.get("enabled"):
        return []
    # Settle lingering continuation state first: relink durable children,
    # expire stale claims, and mark groups whose continuation task already
    # reached a terminal status as completed. Without this sweep a finished
    # continuation stayed "running" forever and blocked the group from ever
    # continuing again (readiness reports already_requested).
    from .continuation_recovery import recoverSubagentLaunchGroupContinuations

    try:
        await recoverSubagentLaunchGroupContinuations(parentSessionId)
    except Exception as exc:
        _emit(runtime, "subagent.continuation.failed", {
            "session_id": parentSessionId,
            "phase": "recovery_sweep",
            "error": str(exc)[:512],
        })
    groups = await _read_subagent_launch_groups(parentSessionId)
    if launchGroupId:
        groups = [group for group in groups if group.get("id") == launchGroupId]
    records = await readSubagentReconciliations(parentSessionId)
    unconsumed_records = await listUnconsumedSubagentReconciliations(parentSessionId)
    started: list[dict[str, Any]] = []
    for group in groups:
        outcome = await _maybe_start_group_continuation(
            parentSessionId,
            group,
            records,
            unconsumed_records,
            policy=policy,
            starter=starter,
            runtime=runtime,
        )
        if outcome:
            started.append(outcome)
    return started


async def _maybe_start_group_continuation(
    parent_session_id: str,
    group: dict[str, Any],
    records: list[dict[str, Any]],
    unconsumed_records: list[dict[str, Any]],
    *,
    policy: dict[str, Any],
    starter: Any = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    group_id = _string(group.get("id"))
    if not group_id:
        return None
    unconsumed_for_group = [record for record in unconsumed_records if _record_belongs_to_group(record, group)]
    if group.get("continuationTaskId"):
        return {"started": False, "reason": "already_requested", "launchGroupId": group_id, "continuationTaskId": group.get("continuationTaskId")}
    if group.get("continuationRequestedAt") and not unconsumed_for_group:
        return {"started": False, "reason": "satisfied_intent", "launchGroupId": group_id}
    if group.get("continuationRequestedAt") and int(group.get("continuationAttemptCount") or 0) >= int(policy.get("maxRecoveryAttempts") or 3):
        return {"started": False, "reason": "recovery_exhausted", "launchGroupId": group_id}
    ready = evaluateParentContinuationReadiness(
        records,
        {
            "waitFor": policy.get("waitFor") or policy.get("wait_for") or "all",
            "quorumCount": policy.get("quorumCount") or policy.get("quorum_count"),
            "group": group,
        },
    )
    if not ready.get("ready"):
        return {"started": False, "launchGroupId": group_id, **ready}
    if not unconsumed_for_group:
        return {"started": False, "launchGroupId": group_id, **ready, "reason": "satisfied_intent"}
    prompt = _string(policy.get("prompt")) or defaultParentContinuationPrompt()
    run_id = _string(group.get("continuationRunId")) or f"continuation-{uuid.uuid4()}"
    parent = await loadSession(parent_session_id)
    if not parent:
        _emit(runtime, "subagent.continuation.failed", {"session_id": parent_session_id, "launch_group_id": group_id, "continuation_run_id": run_id, "error": "Parent session not found."})
        return {"started": False, "reason": "parent_session_not_found", "launchGroupId": group_id, "continuationRunId": run_id}
    task_starter = starter or await _resolve_continuation_starter()
    if not task_starter:
        _emit(runtime, "subagent.continuation.failed", {"session_id": parent_session_id, "launch_group_id": group_id, "continuation_run_id": run_id, "error": "No task supervisor available."})
        return {"started": False, "reason": "supervisor_unavailable", "launchGroupId": group_id, "continuationRunId": run_id}
    from .coordination import (
        claimSubagentLaunchGroupContinuation,
        markSubagentLaunchGroupContinuationRunning,
        markSubagentLaunchGroupContinuationStarting,
        releaseSubagentLaunchGroupContinuationClaim,
    )

    claim = await claimSubagentLaunchGroupContinuation(
        parentSessionId=parent_session_id,
        launchGroupId=group_id,
        claimantId=run_id,
    )
    if not claim:
        return {"started": False, "reason": "already_requested", "launchGroupId": group_id}
    claim_token = claim["claimToken"]
    starting = await markSubagentLaunchGroupContinuationStarting(
        parent_session_id, group_id, claim_token
    )
    if not starting:
        return {"started": False, "reason": "claim_lost", "launchGroupId": group_id}
    _emit(runtime, "subagent.continuation.requested", {"session_id": parent_session_id, "launch_group_id": group_id, "continuation_run_id": run_id, **ready})
    try:
        launched = await task_starter(
            {
                "sessionId": parent_session_id,
                "cwd": parent.get("cwd"),
                "prompt": prompt,
                "title": f"Parent continuation for {group.get('label') or group_id}",
                "runtimeHome": policy.get("runtimeHome"),
            }
        )
        task_id = _launch_task_id(launched)
        if task_id:
            linked = await markSubagentLaunchGroupContinuationRunning(
                parent_session_id, group_id, claim_token, task_id
            )
            if not linked:
                raise RuntimeError("Continuation started durably but its launch-group claim could not be linked.")
        _emit(runtime, "subagent.continuation.started", {"session_id": parent_session_id, "launch_group_id": group_id, "continuation_run_id": run_id, "task_id": task_id})
        return {"started": True, "launchGroupId": group_id, "continuationRunId": run_id, "continuationTaskId": task_id, **ready}
    except Exception as exc:
        await releaseSubagentLaunchGroupContinuationClaim(
            parent_session_id, group_id, claim_token, str(exc)
        )
        _emit(runtime, "subagent.continuation.failed", {"session_id": parent_session_id, "launch_group_id": group_id, "continuation_run_id": run_id, "error": str(exc)})
        return {"started": False, "reason": "start_failed", "launchGroupId": group_id, "continuationRunId": run_id, "error": str(exc)}


async def _resolve_continuation_starter() -> Any:
    try:
        from ..daemon import connectTaskSupervisorDaemonClient

        client = await connectTaskSupervisorDaemonClient()
        if client and callable(client.get("startMainSessionTask")):
            return client["startMainSessionTask"]
    except Exception:
        return None
    return None


def _record_belongs_to_group(record: dict[str, Any], group: dict[str, Any]) -> bool:
    child_ids = {child_id for child_id in (group.get("childAgentIds") or []) if isinstance(child_id, str)}
    task_ids = {task_id for task_id in (group.get("taskIds") or []) if isinstance(task_id, str)}
    return record.get("launchGroupId") == group.get("id") or record.get("childAgentId") in child_ids or record.get("taskId") in task_ids


