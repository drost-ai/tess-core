from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knobValue

    return knobValue(name)


from ._shared import withSubagentCoordinationTransaction

import copy
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .._utils import now_iso
from . import groups as _groups
from . import reconciliation as _reconciliations






async def listSubagentReconciliations(parentSessionId: str) -> list[dict[str, Any]]:
    return await _reconciliations.readSubagentReconciliations(parentSessionId)


async def claimUnconsumedSubagentReconciliations(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    values = {**(args or {}), **kwargs}
    parent_id = _required(values, "parentSessionId")
    consumer_id = _required(values, "consumerId")
    now = values.get("now") or now_iso()
    now_dt = _parse_time(now)
    lease_ms = max(1, int(values.get("leaseMs") or int(_knob("subagents.continuation_lease_ms"))))
    token = str(uuid.uuid4())
    claimed: list[dict[str, Any]] = []
    path = _reconciliations.subagentReconciliationLogPath(parent_id)
    async with withSubagentCoordinationTransaction(parent_id):
        records = await _reconciliations._read_records(path)
        next_records: list[dict[str, Any]] = []
        for record in records:
            claim = record.get("consumptionClaim") if isinstance(record.get("consumptionClaim"), dict) else {}
            expires = _try_parse_time(claim.get("leaseExpiresAt"))
            if record.get("consumedAt") or record.get("parentMessageAppendedAt") or (expires and expires > now_dt):
                next_records.append(record)
                continue
            updated = {
                **record,
                "revision": _revision(record) + 1,
                "consumptionClaim": {
                    "schemaVersion": 1,
                    "token": token,
                    "consumerId": consumer_id,
                    "claimedAt": now,
                    "leaseExpiresAt": _format_time(now_dt + timedelta(milliseconds=lease_ms)),
                },
            }
            claimed.append(copy.deepcopy(updated))
            next_records.append(updated)
        if claimed:
            await _reconciliations._write_records(path, _reconciliations._sort_records(next_records))
    return {"claimToken": token, "records": claimed} if claimed else None


async def completeSubagentReconciliationClaim(
    parentSessionId: str,
    claimToken: str,
    consumedAt: str | None = None,
    *,
    parentMessageIndex: int | None = None,
    parentTurnId: str | None = None,
) -> list[dict[str, Any]]:
    consumed_at = consumedAt or now_iso()
    completed: list[dict[str, Any]] = []
    path = _reconciliations.subagentReconciliationLogPath(parentSessionId)
    async with withSubagentCoordinationTransaction(parentSessionId):
        records = await _reconciliations._read_records(path)
        next_records = []
        for record in records:
            claim = record.get("consumptionClaim") if isinstance(record.get("consumptionClaim"), dict) else {}
            if record.get("consumedAt") or claim.get("token") != claimToken:
                next_records.append(record)
                continue
            updated = {
                **record,
                "revision": _revision(record) + 1,
                "consumedAt": consumed_at,
                "parentMessageAppendedAt": record.get("parentMessageAppendedAt") or consumed_at,
                "parentMessageIndex": record.get("parentMessageIndex") if record.get("parentMessageIndex") is not None else parentMessageIndex,
                "parentTurnId": record.get("parentTurnId") or parentTurnId,
            }
            updated.pop("consumptionClaim", None)
            completed.append(copy.deepcopy(updated))
            next_records.append(updated)
        if completed:
            await _reconciliations._write_records(path, _reconciliations._sort_records(next_records))
    return completed




async def listSubagentLaunchGroups(parentSessionId: str) -> list[dict[str, Any]]:
    return await _groups._read_subagent_launch_groups(parentSessionId)


async def upsertSubagentLaunchGroup(record: dict[str, Any]) -> dict[str, Any]:
    return await _groups._upsert_subagent_launch_group(record["parentSessionId"], record)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _try_parse_time(value: Any) -> datetime | None:
    try:
        return _parse_time(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _required(values: dict[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required.")
    return value


def _revision(record: dict[str, Any]) -> int:
    value = record.get("revision")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _has_delivered_reconciliation(session: dict[str, Any], record: dict[str, Any]) -> bool:
    record_id = str(record.get("id") or "").strip()
    state = session.get("metadata", {}).get("subagentReconciliation") or {}
    delivered = state.get("deliveredRecordIds")
    if record_id and isinstance(delivered, list):
        # The structural ledger is authoritative once present. Deliveries that
        # predate it are covered by lastInjectedRecordIds; the fragile text
        # scan only remains for sessions that have never carried either.
        legacy_injected = state.get("lastInjectedRecordIds") if isinstance(state.get("lastInjectedRecordIds"), list) else []
        return record_id in delivered or record_id in legacy_injected
    return _has_delivered_reconciliation_text(session, record)


def _has_delivered_reconciliation_text(session: dict[str, Any], record: dict[str, Any]) -> bool:
    task_id = str(record.get("taskId") or "").strip()
    child_id = str(record.get("childAgentId") or "").strip()
    for message in session.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or "[Background subagent updates]" not in content:
            continue
        if task_id and f"Task: {task_id}" not in content:
            continue
        if child_id and f"Child: {child_id}" not in content:
            continue
        return True
    return False


def _bound_reason(reason: Any) -> str:
    value = str(reason)
    return value if len(value) <= 500 else f"{value[:497]}..."


