from __future__ import annotations

import json
import os
import uuid
from typing import Any

from .._utils import now_iso
from ..session.message_origin import syntheticUserMessage as _synthetic_user_message
from ..storage import sessionArchiveDir, taskOutputPath, withRecordTransaction


TERMINAL_RECONCILIATION_STATUSES = {"completed", "failed", "stopped", "timed_out"}
_SUMMARY_LIMIT = 1200
def subagentReconciliationLogPath(parentSessionId: str) -> str:
    return os.path.join(sessionArchiveDir(parentSessionId), "subagent-reconciliations.jsonl")

async def readSubagentReconciliations(parentSessionId: str) -> list[dict[str, Any]]:
    return await _read_records(subagentReconciliationLogPath(parentSessionId))

async def listUnconsumedSubagentReconciliations(parentSessionId: str) -> list[dict[str, Any]]:
    records = await readSubagentReconciliations(parentSessionId)
    return [
        record
        for record in records
        if not record.get("consumedAt") and not record.get("parentMessageAppendedAt")
    ]


async def upsertSubagentReconciliation(record: dict[str, Any]) -> dict[str, Any]:
    parent_session_id = _string(record.get("parentSessionId"))
    if not parent_session_id:
        raise ValueError("Subagent reconciliation requires parentSessionId.")
    record_id = _string(record.get("id")) or _build_record_id(record)
    idempotency_key = _string(record.get("idempotencyKey")) or record_id
    now = now_iso()
    normalized = {
        **record,
        "id": record_id,
        "idempotencyKey": idempotency_key,
        "parentSessionId": parent_session_id,
        "createdAt": _string(record.get("createdAt")) or now,
        "updatedAt": now,
    }
    path = subagentReconciliationLogPath(parent_session_id)

    async with withRecordTransaction("subagent-coordination", parent_session_id):
        records = await _read_records(path)
        replaced = False
        next_records: list[dict[str, Any]] = []
        for existing in records:
            existing_key = _string(existing.get("idempotencyKey")) or _string(existing.get("id"))
            if existing_key and existing_key == idempotency_key:
                merged = {**existing, **normalized}
                for key in (
                    "consumedAt",
                    "parentMessageAppendedAt",
                    "parentMessageIndex",
                    "parentTurnId",
                    "continuationStartedAt",
                    "continuationCompletedAt",
                    "continuationFailedAt",
                    "continuationError",
                ):
                    if existing.get(key) is not None and normalized.get(key) is None:
                        merged[key] = existing[key]
                normalized = merged
                next_records.append(merged)
                replaced = True
            else:
                next_records.append(existing)
        if not replaced:
            next_records.append(normalized)
        await _write_records(path, _sort_records(next_records))
    return normalized


async def markSubagentReconciliationsConsumed(
    parentSessionId: str,
    recordIds: list[str],
    *,
    parentMessageIndex: int | None = None,
    parentTurnId: str | None = None,
) -> list[dict[str, Any]]:
    wanted = {item for item in recordIds if isinstance(item, str) and item}
    if not wanted:
        return []
    path = subagentReconciliationLogPath(parentSessionId)
    consumed_at = now_iso()
    changed: list[dict[str, Any]] = []
    async with withRecordTransaction("subagent-coordination", parentSessionId):
        records = await _read_records(path)
        next_records = []
        for record in records:
            record_keys = {_string(record.get("id")), _string(record.get("idempotencyKey"))}
            if wanted.intersection(record_keys):
                record = {
                    **record,
                    "consumedAt": record.get("consumedAt") or consumed_at,
                    "parentMessageAppendedAt": record.get("parentMessageAppendedAt") or consumed_at,
                    "parentMessageIndex": record.get("parentMessageIndex") if record.get("parentMessageIndex") is not None else parentMessageIndex,
                    "parentTurnId": record.get("parentTurnId") or parentTurnId,
                    "updatedAt": consumed_at,
                }
                changed.append(record)
            next_records.append(record)
        await _write_records(path, _sort_records(next_records))
    return changed


async def createSubagentReconciliationForTerminalChild(
    *,
    childRecord: dict[str, Any] | None = None,
    taskMeta: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: Any = None,
    source: str = "terminal_finalizer",
) -> dict[str, Any] | None:
    record = buildSubagentReconciliationRecord(
        childRecord=childRecord,
        taskMeta=taskMeta,
        session=session,
        result=result,
        error=error,
        source=source,
    )
    if not record:
        return None
    return await upsertSubagentReconciliation(record)


def buildSubagentReconciliationRecord(
    *,
    childRecord: dict[str, Any] | None = None,
    taskMeta: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: Any = None,
    source: str = "terminal_finalizer",
) -> dict[str, Any] | None:
    child = childRecord if isinstance(childRecord, dict) else {}
    meta = taskMeta if isinstance(taskMeta, dict) else {}
    child_session_metadata = session.get("metadata") if isinstance(session, dict) and isinstance(session.get("metadata"), dict) else {}
    parent_session_id = _first_string(
        child.get("parentSessionId"),
        meta.get("parentSessionId"),
        meta.get("parent_session_id"),
        child_session_metadata.get("parentSessionId"),
    )
    if not parent_session_id:
        return None
    child_agent_id = _first_string(child.get("agentId"), meta.get("agent_id"), meta.get("agentId"))
    child_session_id = _first_string(
        child.get("sessionId"),
        meta.get("session_id"),
        meta.get("sessionId"),
        session.get("id") if isinstance(session, dict) else None,
    )
    task_id = _first_string(child.get("taskId"), meta.get("task_id"), meta.get("taskId"))
    terminal_status = _terminal_status_from_values(child, meta, result, error)
    if terminal_status not in TERMINAL_RECONCILIATION_STATUSES:
        return None
    completed_at = _first_string(
        child.get("completedAt"),
        child.get("latestResultAt"),
        meta.get("completedAt"),
        meta.get("finishedAt"),
        meta.get("updatedAt"),
    ) or now_iso()
    reason = _clean_text(
        _first_string(
            (result or {}).get("stopReason") if isinstance(result, dict) else None,
            child.get("completionReason"),
            meta.get("completionReason"),
            child.get("stopReason"),
            meta.get("stopReason"),
            child.get("waitingReason"),
            meta.get("waitingReason"),
            child.get("error"),
            meta.get("error"),
            str(error) if error is not None else None,
        ),
        280,
    )
    summary = _summary_from_sources(child, meta, result, error)
    label = _first_string(
        child.get("name"),
        meta.get("title"),
        child.get("agentType"),
        child.get("teamName"),
        child_agent_id,
        task_id,
    ) or "background subagent"
    output_ref = _first_string(meta.get("outputFile"), meta.get("output_file"))
    if not output_ref and task_id:
        output_ref = taskOutputPath(task_id)
    terminal_version = _first_string(meta.get("terminalVersion"), meta.get("terminal_version")) or "v1"
    idempotency_key = ":".join(
        [
            "subagent-reconciliation",
            parent_session_id,
            child_agent_id or "unknown-agent",
            task_id or "unknown-task",
            terminal_status,
            terminal_version,
        ]
    )
    return _without_none(
        {
            "id": f"rec-{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)}",
            "idempotencyKey": idempotency_key,
            "parentSessionId": parent_session_id,
            "parentAgentId": _first_string(child.get("parentAgentId"), child_session_metadata.get("parentAgentId")),
            "childSessionId": child_session_id,
            "childAgentId": child_agent_id,
            "taskId": task_id,
            "parentTaskId": _first_string(child.get("parentTaskId"), meta.get("parent_task_id"), meta.get("parentTaskId")),
            "parentToolUseId": _first_string(child.get("parentToolUseId"), meta.get("parent_tool_use_id"), meta.get("parentToolUseId")),
            "launchGroupId": _first_string(child.get("launchGroupId"), meta.get("launch_group_id"), meta.get("launchGroupId")),
            "label": label,
            "agentType": _first_string(child.get("agentType"), meta.get("agentType")),
            "teamName": _first_string(child.get("teamName"), meta.get("teamName")),
            "terminalStatus": terminal_status,
            "terminalReason": reason,
            "summary": summary,
            "outputRef": output_ref,
            "taskOutputRef": f"task-output://{task_id}/final" if task_id else None,
            "structuredResultStatus": _first_string(child.get("structuredResultStatus"), meta.get("structuredResultStatus")),
            "usage": (result or {}).get("usage") if isinstance((result or {}).get("usage"), dict) else None,
            "childCompletedAt": completed_at,
            "source": source,
        }
    )


def renderSubagentReconciliationMessage(records: list[dict[str, Any]]) -> str:
    lines = ["[Background subagent updates]"]
    for index, record in enumerate(_sort_records(records), start=1):
        status = _string(record.get("terminalStatus")) or "completed"
        title = _string(record.get("label")) or _string(record.get("childAgentId")) or "subagent"
        task_id = _string(record.get("taskId"))
        child_id = _string(record.get("childAgentId"))
        summary = _clean_text(record.get("summary") or record.get("terminalReason") or "No final output available.", _SUMMARY_LIMIT)
        reason = _clean_text(record.get("terminalReason"), 280)
        output_ref = _string(record.get("taskOutputRef")) or _string(record.get("outputRef"))
        lines.append("")
        lines.append(f"{index}. {status.replace('_', ' ').title()}: {title}")
        if task_id:
            lines.append(f"   Task: {task_id}")
        if child_id:
            lines.append(f"   Child: {child_id}")
        if reason and status != "completed":
            lines.append(f"   Reason: {reason}")
        lines.append(f"   Summary: {summary}")
        if output_ref:
            lines.append(f"   Output: {output_ref}")
    lines.append("")
    lines.append("[/Background subagent updates]")
    return "\n".join(lines)


async def appendPendingSubagentReconciliationsToSession(
    session: dict[str, Any],
    *,
    runtime: dict[str, Any] | None = None,
    parentTurnId: str | None = None,
) -> dict[str, Any]:
    from .coordination import (
        _has_delivered_reconciliation,
        claimUnconsumedSubagentReconciliations,
        completeSubagentReconciliationClaim,
    )

    claim = await claimUnconsumedSubagentReconciliations(
        parentSessionId=session["id"],
        consumerId=f"parent-delivery:{os.getpid()}:{uuid.uuid4()}",
    )
    if not claim:
        return {"records": [], "messageIndex": None}
    records = [record for record in claim["records"] if not _has_delivered_reconciliation(session, record)]
    if not records:
        await completeSubagentReconciliationClaim(session["id"], claim["claimToken"])
        return {"records": [], "messageIndex": None}
    message_index = len(session.setdefault("messages", []))
    text = renderSubagentReconciliationMessage(records)
    session["messages"].append(_synthetic_user_message(text, actor="subagent", kind="subagent_reconciliation", lineage={"logicalTurnId": str(parentTurnId or "")}))
    metadata = session.setdefault("metadata", {})
    metadata["subagentReconciliation"] = {
        "lastInjectedAt": now_iso(),
        "lastInjectedCount": len(records),
        "lastInjectedRecordIds": [record.get("id") for record in records if record.get("id")],
        "pendingBeforeInjection": len(records),
    }
    _emit(runtime, "subagent.reconciliation.appended", {"session_id": session["id"], "count": len(records), "record_ids": metadata["subagentReconciliation"]["lastInjectedRecordIds"]})
    return {
        "records": records,
        "claimedRecords": claim["records"],
        "claimToken": claim["claimToken"],
        "messageIndex": message_index,
        "parentTurnId": parentTurnId,
    }


async def consumeAppendedSubagentReconciliations(
    session: dict[str, Any],
    appended: dict[str, Any],
    *,
    runtime: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from .coordination import completeSubagentReconciliationClaim

    claim_token = appended.get("claimToken") if isinstance(appended, dict) else None
    if isinstance(claim_token, str) and claim_token:
        consumed = await completeSubagentReconciliationClaim(
            session["id"],
            claim_token,
            parentMessageIndex=appended.get("messageIndex"),
            parentTurnId=appended.get("parentTurnId"),
        )
        if consumed:
            _emit(runtime, "subagent.reconciliation.consumed", {"session_id": session["id"], "count": len(consumed), "record_ids": [record.get("id") for record in consumed]})
        return consumed
    records = appended.get("records") if isinstance(appended, dict) else []
    record_ids = [record.get("id") or record.get("idempotencyKey") for record in records if isinstance(record, dict)]
    consumed = await markSubagentReconciliationsConsumed(
        session["id"],
        [record_id for record_id in record_ids if isinstance(record_id, str) and record_id],
        parentMessageIndex=appended.get("messageIndex") if isinstance(appended, dict) else None,
        parentTurnId=appended.get("parentTurnId") if isinstance(appended, dict) else None,
    )
    if consumed:
        _emit(runtime, "subagent.reconciliation.consumed", {"session_id": session["id"], "count": len(consumed), "record_ids": [record.get("id") for record in consumed]})
    return consumed


def _terminal_status_from_values(
    child: dict[str, Any],
    meta: dict[str, Any],
    result: dict[str, Any] | None,
    error: Any,
) -> str:
    raw = _first_string(
        child.get("status"),
        meta.get("status"),
        (result or {}).get("stopReason") if isinstance(result, dict) else None,
        "failed" if error is not None else None,
    ).lower()
    reason = " ".join(
        [
            _string((result or {}).get("stopReason") if isinstance(result, dict) else None),
            _string(child.get("completionReason")),
            _string(meta.get("completionReason")),
            _string(child.get("error")),
            _string(meta.get("error")),
            _string(error),
        ]
    ).lower()
    if "timeout" in raw or "timed" in raw or "timeout" in reason or "timed out" in reason:
        return "timed_out"
    if raw in {"completed", "end_turn", "success", "succeeded"}:
        return "completed"
    if raw in {"cancelled", "canceled", "stopped", "terminated", "no_progress"}:
        return "stopped"
    return "failed"


def _summary_from_sources(
    child: dict[str, Any],
    meta: dict[str, Any],
    result: dict[str, Any] | None,
    error: Any,
) -> str:
    candidates = [
        child.get("structuredResult"),
        meta.get("structuredResult"),
        (result or {}).get("text") if isinstance(result, dict) else None,
        child.get("resultSummary"),
        meta.get("resultSummary"),
        child.get("lastSummary"),
        meta.get("summary"),
        child.get("error"),
        meta.get("error"),
        str(error) if error is not None else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            text = _structured_summary(candidate)
        else:
            text = _clean_text(candidate, _SUMMARY_LIMIT)
        if text:
            return text
    return "No final output available."


def _structured_summary(value: dict[str, Any]) -> str:
    for key in ("summary", "result", "answer", "text", "content"):
        text = _clean_text(value.get(key), _SUMMARY_LIMIT)
        if text:
            return text
    try:
        return _clean_text(json.dumps(value, ensure_ascii=False, sort_keys=True), _SUMMARY_LIMIT)
    except Exception:
        return ""


async def _read_records(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


async def _write_records(path: str, records: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for record in records:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
    os.replace(tmp, path)


def _sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda item: (_string(item.get("launchGroupId")), _string(item.get("childCompletedAt")), _string(item.get("createdAt")), _string(item.get("id"))))


def _build_record_id(record: dict[str, Any]) -> str:
    key = _string(record.get("idempotencyKey")) or ":".join(
        [
            _string(record.get("parentSessionId")),
            _string(record.get("childAgentId")),
            _string(record.get("taskId")),
            _string(record.get("terminalStatus")),
        ]
    )
    return f"rec-{uuid.uuid5(uuid.NAMESPACE_URL, key)}"


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.strip().split())
    if len(text) > limit:
        return f"{text[: max(0, limit - 3)].rstrip()}..."
    return text


def _first_string(*values: Any) -> str:
    for value in values:
        text = _string(value)
        if text:
            return text
    return ""


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None and item != ""}


def _emit(runtime: dict[str, Any] | None, event: str, payload: dict[str, Any]) -> None:
    if not isinstance(runtime, dict):
        return
    emit = runtime.get("emitEvent")
    if callable(emit):
        try:
            emit(event, payload)
        except Exception:
            pass


subagent_reconciliation_log_path = subagentReconciliationLogPath
read_subagent_reconciliations = readSubagentReconciliations
list_unconsumed_subagent_reconciliations = listUnconsumedSubagentReconciliations
upsert_subagent_reconciliation = upsertSubagentReconciliation
mark_subagent_reconciliations_consumed = markSubagentReconciliationsConsumed
create_subagent_reconciliation_for_terminal_child = createSubagentReconciliationForTerminalChild
build_subagent_reconciliation_record = buildSubagentReconciliationRecord
render_subagent_reconciliation_message = renderSubagentReconciliationMessage
append_pending_subagent_reconciliations_to_session = appendPendingSubagentReconciliationsToSession
consume_appended_subagent_reconciliations = consumeAppendedSubagentReconciliations
