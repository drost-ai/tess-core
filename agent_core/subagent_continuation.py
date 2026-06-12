from __future__ import annotations

import uuid
from typing import Any

from ._utils import now_iso
from .storage import fileLock, loadSession, saveSession
from .subagent_reconciliation import (
    TERMINAL_RECONCILIATION_STATUSES,
    _read_records,
    _sort_records,
    _write_records,
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
    records = await listUnconsumedSubagentReconciliations(parentSessionId)
    if launchGroupId:
        records = [record for record in records if record.get("launchGroupId") == launchGroupId]
    ready = evaluateParentContinuationReadiness(records, {"waitFor": waitFor})
    if not ready["ready"]:
        return {"requested": False, **ready}
    requested_at = now_iso()
    run_id = f"continuation-{uuid.uuid4()}"
    path = subagentReconciliationLogPath(parentSessionId)
    wanted = {record.get("id") for record in records if record.get("id")}
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
    return "Continue from these completed background subagent results. Synthesize the findings, note uncertainty, and decide the next action."


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
