from __future__ import annotations

import uuid
from typing import Any

from ._utils import now_iso
from .contracts.records import launch_task_id as _launch_task_id
from .storage import fileLock, loadSession, saveSession
from .subagent_groups import (
    _mark_subagent_launch_group_continuation_requested,
    _read_subagent_launch_groups,
    _record_subagent_launch_group_continuation_task,
)
from .subagent_reconciliation import (
    TERMINAL_RECONCILIATION_STATUSES,
    _read_records,
    _sort_records,
    _write_records,
    readSubagentReconciliations,
    listUnconsumedSubagentReconciliations,
    subagentReconciliationLogPath,
)


async def requestParentContinuationForReconciliations(
    parentSessionId: str,
    *,
    waitFor: str = "manual",
    prompt: str | None = None,
    launchGroupId: str | None = None,
) -> dict[str, Any]:
    records = await readSubagentReconciliations(parentSessionId)
    unconsumed_records = await listUnconsumedSubagentReconciliations(parentSessionId)
    if launchGroupId:
        records = [record for record in records if record.get("launchGroupId") == launchGroupId]
        unconsumed_records = [record for record in unconsumed_records if record.get("launchGroupId") == launchGroupId]
    readiness_records = records if launchGroupId else unconsumed_records
    ready = evaluateParentContinuationReadiness(readiness_records, {"waitFor": waitFor})
    if not ready["ready"]:
        return {"requested": False, **ready}
    requested_at = now_iso()
    run_id = f"continuation-{uuid.uuid4()}"
    path = subagentReconciliationLogPath(parentSessionId)
    wanted = {record.get("id") for record in unconsumed_records if record.get("id")}
    if not wanted:
        return {"requested": False, **ready, "reason": "satisfied_intent"}
    async with fileLock(path):
        current = await _read_records(path)
        next_records = []
        for record in current:
            if record.get("id") in wanted:
                record = {
                    **record,
                    "continuationRequestedAt": record.get("continuationRequestedAt") or requested_at,
                    "continuationRunId": record.get("continuationRunId") or run_id,
                    "continuationPrompt": prompt or defaultParentContinuationPrompt(),
                    "updatedAt": requested_at,
                }
            next_records.append(record)
        await _write_records(path, _sort_records(next_records))
    return {"requested": True, "continuationRunId": run_id, "recordIds": sorted(wanted), **ready}


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


async def runParentContinuationFromReconciliations(
    parentSessionId: str,
    config: dict[str, Any],
    runtime: dict[str, Any],
    *,
    prompt: str | None = None,
    providerOverride: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = await loadSession(parentSessionId)
    if not session:
        raise RuntimeError(f"Parent session not found: {parentSessionId}")
    _emit(runtime, "subagent.continuation.started", {"session_id": parentSessionId})
    from .agent import runAgentPrompt

    result = await runAgentPrompt(
        {
            "session": session,
            "config": config,
            "runtime": runtime,
            "prompt": prompt or defaultParentContinuationPrompt(),
            "providerOverride": providerOverride,
        }
    )
    await saveSession(session)
    _emit(runtime, "subagent.continuation.completed", {"session_id": parentSessionId, "stop_reason": result.get("stopReason")})
    return result


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
    await _mark_subagent_launch_group_continuation_requested(parent_session_id, group_id, prompt=prompt, runId=run_id)
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
            await _record_subagent_launch_group_continuation_task(parent_session_id, group_id, task_id, runId=run_id)
        _emit(runtime, "subagent.continuation.started", {"session_id": parent_session_id, "launch_group_id": group_id, "continuation_run_id": run_id, "task_id": task_id})
        return {"started": True, "launchGroupId": group_id, "continuationRunId": run_id, "continuationTaskId": task_id, **ready}
    except Exception as exc:
        _emit(runtime, "subagent.continuation.failed", {"session_id": parent_session_id, "launch_group_id": group_id, "continuation_run_id": run_id, "error": str(exc)})
        return {"started": False, "reason": "start_failed", "launchGroupId": group_id, "continuationRunId": run_id, "error": str(exc)}


async def _resolve_continuation_starter() -> Any:
    try:
        from .daemon import connectTaskSupervisorDaemonClient

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


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _emit(runtime: dict[str, Any] | None, event: str, payload: dict[str, Any]) -> None:
    if not isinstance(runtime, dict):
        return
    emit = runtime.get("emitEvent")
    if callable(emit):
        try:
            emit(event, payload)
        except Exception:
            pass


evaluate_parent_continuation_readiness = evaluateParentContinuationReadiness
request_parent_continuation_for_reconciliations = requestParentContinuationForReconciliations
run_parent_continuation_from_reconciliations = runParentContinuationFromReconciliations
default_parent_continuation_prompt = defaultParentContinuationPrompt
