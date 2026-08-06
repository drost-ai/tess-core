from __future__ import annotations

import contextlib
import copy
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

from .._utils import now_iso
from ..process.identity import captureProcessBirthMarker
from ..storage import withRecordTransaction
from . import groups as _groups
from . import reconciliation as _reconciliations


DEFAULT_CONTINUATION_LEASE_MS = 30_000


@contextlib.asynccontextmanager
async def withSubagentCoordinationTransaction(parentSessionId: str) -> AsyncIterator[None]:
    async with withRecordTransaction("subagent-coordination", parentSessionId):
        yield


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
    lease_ms = max(1, int(values.get("leaseMs") or DEFAULT_CONTINUATION_LEASE_MS))
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


async def claimSubagentLaunchGroupContinuation(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    values = {**(args or {}), **kwargs}
    parent_id = _required(values, "parentSessionId")
    group_id = _required(values, "launchGroupId")
    now = values.get("now") or now_iso()
    now_dt = _parse_time(now)
    token = str(uuid.uuid4())
    try:
        birth = await captureProcessBirthMarker()
    except Exception:
        birth = None
    selected: dict[str, Any] | None = None
    async with withSubagentCoordinationTransaction(parent_id):
        records = await _groups._read_subagent_launch_groups(parent_id)
        next_records = []
        for group in records:
            if group.get("id") != group_id:
                next_records.append(group)
                continue
            state = _normalize_continuation_state(group, now_dt)
            if state.get("phase") not in {"pending", "retryable"}:
                next_records.append(group)
                continue
            continuation = {
                "schemaVersion": 1,
                "phase": "claimed",
                "attempt": int(state.get("attempt") or 0) + 1,
                "revision": int(state.get("revision") or 0) + 1,
                "claimToken": token,
                "claimantId": values.get("claimantId") or f"{os.getpid()}:{token}",
                "claimantPid": os.getpid(),
                "claimedAt": now,
                "leaseExpiresAt": _format_time(
                    now_dt + timedelta(milliseconds=max(1, int(values.get("leaseMs") or DEFAULT_CONTINUATION_LEASE_MS)))
                ),
            }
            if birth:
                continuation.update({
                    "claimantStartToken": birth.get("startToken"),
                    "claimantPlatform": birth.get("platform"),
                })
            selected = {**group, "continuation": continuation, "updatedAt": now}
            next_records.append(selected)
        if selected:
            await _groups._write_subagent_launch_groups(
                _groups._subagent_launch_groups_path(parent_id),
                _groups._sort_groups(next_records),
            )
    return {"group": selected, "claimToken": token} if selected else None


async def markSubagentLaunchGroupContinuationStarting(
    parentSessionId: str,
    launchGroupId: str,
    claimToken: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    timestamp = now or now_iso()
    return await _transition_claim(
        parentSessionId,
        launchGroupId,
        claimToken,
        lambda state: {
            **state,
            "phase": "starting",
            "revision": int(state.get("revision") or 0) + 1,
            "startingAt": state.get("startingAt") or timestamp,
            "leaseExpiresAt": _format_time(_parse_time(timestamp) + timedelta(milliseconds=DEFAULT_CONTINUATION_LEASE_MS)),
        },
        timestamp,
    )


async def markSubagentLaunchGroupContinuationRunning(
    parentSessionId: str,
    launchGroupId: str,
    claimToken: str,
    childTaskId: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    timestamp = now or now_iso()
    return await _transition_claim(
        parentSessionId,
        launchGroupId,
        claimToken,
        lambda state: {
            **state,
            "phase": "running",
            "revision": int(state.get("revision") or 0) + 1,
            "childTaskId": childTaskId,
            "leaseExpiresAt": None,
        },
        timestamp,
        childTaskId,
    )


async def releaseSubagentLaunchGroupContinuationClaim(
    parentSessionId: str,
    launchGroupId: str,
    claimToken: str,
    reason: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    timestamp = now or now_iso()
    return await _transition_claim(
        parentSessionId,
        launchGroupId,
        claimToken,
        lambda state: {
            "schemaVersion": 1,
            "phase": "retryable",
            "attempt": int(state.get("attempt") or 0),
            "revision": int(state.get("revision") or 0) + 1,
            "failureReason": _bound_reason(reason),
        },
        timestamp,
    )


async def markSubagentLaunchGroupContinuationCompleted(
    parentSessionId: str,
    launchGroupId: str,
    childTaskId: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    details = details or {}
    completed_at = details.get("completedAt") or now_iso()
    selected: dict[str, Any] | None = None
    async with withSubagentCoordinationTransaction(parentSessionId):
        records = await _groups._read_subagent_launch_groups(parentSessionId)
        next_records = []
        changed = False
        for group in records:
            if group.get("id") != launchGroupId:
                next_records.append(group)
                continue
            state = group.get("continuation") if isinstance(group.get("continuation"), dict) else {}
            if state.get("phase") == "completed" and state.get("childTaskId") == childTaskId:
                selected = group
                next_records.append(group)
                continue
            linked = (
                state.get("phase") == "running" and state.get("childTaskId") == childTaskId
            ) or group.get("continuationRunId") == childTaskId or group.get("continuationTaskId") == childTaskId
            if not linked:
                next_records.append(group)
                continue
            continuation = {
                "schemaVersion": 1,
                "phase": "completed",
                "attempt": max(1, int(state.get("attempt") or 1)),
                "revision": int(state.get("revision") or 0) + 1,
                "childTaskId": childTaskId,
                "completedAt": completed_at,
            }
            reason = details.get("reason") or state.get("failureReason")
            if reason:
                continuation["failureReason"] = _bound_reason(reason)
            selected = {**group, "continuation": continuation, "updatedAt": completed_at}
            next_records.append(selected)
            changed = True
        if changed:
            await _groups._write_subagent_launch_groups(
                _groups._subagent_launch_groups_path(parentSessionId),
                _groups._sort_groups(next_records),
            )
    return selected


async def _transition_claim(
    parent_id: str,
    group_id: str,
    claim_token: str,
    transition: Callable[[dict[str, Any]], dict[str, Any]],
    now: str,
    continuation_run_id: str | None = None,
) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    async with withSubagentCoordinationTransaction(parent_id):
        records = await _groups._read_subagent_launch_groups(parent_id)
        next_records = []
        for group in records:
            state = group.get("continuation") if isinstance(group.get("continuation"), dict) else None
            if not (
                group.get("id") == group_id
                and state
                and state.get("claimToken") == claim_token
                and state.get("phase") in {"claimed", "starting"}
            ):
                next_records.append(group)
                continue
            continuation = transition(copy.deepcopy(state))
            selected = {
                **group,
                "continuation": {key: value for key, value in continuation.items() if value is not None},
                "updatedAt": now,
            }
            if continuation_run_id:
                selected["continuationRequestedAt"] = group.get("continuationRequestedAt") or now
                selected["continuationRunId"] = continuation_run_id
                selected["continuationTaskId"] = continuation_run_id
            next_records.append(selected)
        if selected:
            await _groups._write_subagent_launch_groups(
                _groups._subagent_launch_groups_path(parent_id),
                _groups._sort_groups(next_records),
            )
    return selected


def _normalize_continuation_state(group: dict[str, Any], now: datetime) -> dict[str, Any]:
    state = group.get("continuation") if isinstance(group.get("continuation"), dict) else None
    if state is None:
        linked = group.get("continuationTaskId") or group.get("continuationRunId")
        if linked:
            return {"schemaVersion": 1, "phase": "running", "attempt": 1, "childTaskId": linked}
        if group.get("continuationRequestedAt"):
            return {"schemaVersion": 1, "phase": "retryable", "attempt": 0}
        return {"schemaVersion": 1, "phase": "pending", "attempt": 0}
    expires = _try_parse_time(state.get("leaseExpiresAt"))
    if state.get("phase") in {"claimed", "starting"} and expires and expires <= now and not state.get("childTaskId"):
        return {
            "schemaVersion": 1,
            "phase": "retryable",
            "attempt": int(state.get("attempt") or 0),
            "revision": int(state.get("revision") or 0),
            "recoveryReason": "expired_pre_start_lease",
        }
    return state


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


with_subagent_coordination_transaction = withSubagentCoordinationTransaction
list_subagent_reconciliations = listSubagentReconciliations
claim_unconsumed_subagent_reconciliations = claimUnconsumedSubagentReconciliations
complete_subagent_reconciliation_claim = completeSubagentReconciliationClaim
list_subagent_launch_groups = listSubagentLaunchGroups
upsert_subagent_launch_group = upsertSubagentLaunchGroup
claim_subagent_launch_group_continuation = claimSubagentLaunchGroupContinuation
mark_subagent_launch_group_continuation_starting = markSubagentLaunchGroupContinuationStarting
mark_subagent_launch_group_continuation_running = markSubagentLaunchGroupContinuationRunning
release_subagent_launch_group_continuation_claim = releaseSubagentLaunchGroupContinuationClaim
mark_subagent_launch_group_continuation_completed = markSubagentLaunchGroupContinuationCompleted
