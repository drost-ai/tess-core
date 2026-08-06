from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from ..process.identity import verifyProcessBirthMarker
from ..storage import listTaskRecords
from .coordination import (
    _bound_reason,
    _format_time,
    _groups,
    _parse_time,
    _try_parse_time,
    withSubagentCoordinationTransaction,
)


LEGACY_CLAIM_GRACE_MS = 30_000
TERMINAL_TASK_STATUSES = {"completed", "failed", "stopped"}


async def recoverSubagentLaunchGroupContinuations(
    parentSessionId: str,
    now: str | None = None,
) -> list[dict[str, Any]]:
    from .._utils import now_iso

    timestamp = now or now_iso()
    now_dt = _parse_time(timestamp)
    async with withSubagentCoordinationTransaction(parentSessionId):
        groups = await _groups._read_subagent_launch_groups(parentSessionId)
        tasks = [task for task in await listTaskRecords() if task.get("sessionId") == parentSessionId]
        claimant_statuses = await _inspect_legacy_claimants(groups)
        recovered: list[dict[str, Any]] = []
        next_groups = []
        for group in groups:
            next_group = _recover_group(
                group,
                tasks,
                claimant_statuses.get(str(group.get("id")), "unknown"),
                timestamp,
                now_dt,
            )
            next_groups.append(next_group)
            if next_group is not group:
                recovered.append(copy.deepcopy(next_group))
        if recovered:
            await _groups._write_subagent_launch_groups(
                _groups._subagent_launch_groups_path(parentSessionId),
                _groups._sort_groups(next_groups),
            )
        return recovered


def _recover_group(
    group: dict[str, Any],
    tasks: list[dict[str, Any]],
    claimant_status: str,
    now: str,
    now_dt: datetime,
) -> dict[str, Any]:
    state = group.get("continuation") if isinstance(group.get("continuation"), dict) else None
    legacy_claim = _is_legacy_claim(group)
    expired_claim = _is_expired_claim(state, now_dt)
    running = bool(state and state.get("phase") == "running")
    if not legacy_claim and not expired_claim and not running:
        return group
    candidates = _find_candidate_tasks(group, tasks)
    if len(candidates) > 1:
        return _update(group, {
            "schemaVersion": 1,
            "phase": "recovery_blocked",
            "attempt": int((state or {}).get("attempt") or 0),
            "revision": int((state or {}).get("revision") or 0) + 1,
            "recoveryReason": "multiple_durable_children",
        }, now)
    if len(candidates) == 1:
        return _recover_durable_child(group, candidates[0], now)
    if running or group.get("continuationRunId") or group.get("continuationTaskId"):
        return _update(group, {
            "schemaVersion": 1,
            "phase": "recovery_blocked",
            "attempt": int((state or {}).get("attempt") or 0),
            "revision": int((state or {}).get("revision") or 0) + 1,
            "recoveryReason": "durable_child_link_missing",
        }, now)
    if expired_claim and not legacy_claim:
        return _retryable(group, now, "expired_pre_start_without_child")
    return _recover_legacy_claim_without_child(group, claimant_status, now, now_dt)


def _recover_durable_child(group: dict[str, Any], task: dict[str, Any], now: str) -> dict[str, Any]:
    state = group.get("continuation") if isinstance(group.get("continuation"), dict) else {}
    terminal = task.get("status") in TERMINAL_TASK_STATUSES
    if terminal:
        continuation = {
            "schemaVersion": 1,
            "phase": "completed",
            "attempt": max(1, int(state.get("attempt") or 1)),
            "revision": int(state.get("revision") or 0) + 1,
            "childTaskId": task["id"],
            "completedAt": task.get("completedAt") or task.get("updatedAt"),
            "recoveryReason": "terminal_durable_child_relinked",
        }
        if task.get("status") != "completed" and task.get("summary"):
            continuation["failureReason"] = _bound_reason(task["summary"])
    else:
        continuation = {
            "schemaVersion": 1,
            "phase": "running",
            "attempt": max(1, int(state.get("attempt") or 1)),
            "revision": int(state.get("revision") or 0) + 1,
            "childTaskId": task["id"],
            "recoveryReason": "durable_child_relinked",
        }
    return _update(group, continuation, now, str(task["id"]))


def _recover_legacy_claim_without_child(
    group: dict[str, Any],
    claimant_status: str,
    now: str,
    now_dt: datetime,
) -> dict[str, Any]:
    state = group.get("continuation") if isinstance(group.get("continuation"), dict) else {}
    grace = _try_parse_time(state.get("migrationGraceExpiresAt"))
    if grace:
        return group if grace > now_dt else _retryable(group, now, "legacy_claimant_grace_expired")
    if claimant_status == "dead":
        return _retryable(group, now, "legacy_claimant_dead_without_child")
    source = _first_timestamp(state.get("startingAt"), state.get("claimedAt"), group.get("continuationRequestedAt"))
    grace_expires = (
        now_dt + timedelta(milliseconds=LEGACY_CLAIM_GRACE_MS)
        if claimant_status == "live" or source is None
        else source + timedelta(milliseconds=LEGACY_CLAIM_GRACE_MS)
    )
    if grace_expires <= now_dt:
        return _retryable(group, now, "legacy_claimant_unverified_after_grace")
    continuation = {
        **state,
        "schemaVersion": 1,
        "phase": "starting" if state.get("phase") == "starting" else "claimed",
        "attempt": int(state.get("attempt") or 0),
        "revision": int(state.get("revision") or 0) + 1,
        "leaseExpiresAt": _format_time(grace_expires),
        "migrationObservedAt": now,
        "migrationGraceExpiresAt": _format_time(grace_expires),
        "recoveryReason": (
            "legacy_claimant_live_grace" if claimant_status == "live" else "legacy_claimant_unknown_grace"
        ),
    }
    return _update(group, continuation, now)


def _retryable(group: dict[str, Any], now: str, reason: str) -> dict[str, Any]:
    state = group.get("continuation") if isinstance(group.get("continuation"), dict) else {}
    return _update(group, {
        "schemaVersion": 1,
        "phase": "retryable",
        "attempt": int(state.get("attempt") or 0),
        "revision": int(state.get("revision") or 0) + 1,
        "recoveryReason": reason,
    }, now)


def _update(
    group: dict[str, Any],
    continuation: dict[str, Any],
    now: str,
    child_task_id: str | None = None,
) -> dict[str, Any]:
    updated = {**group, "continuation": continuation, "updatedAt": now}
    if child_task_id:
        updated["continuationRunId"] = child_task_id
        updated["continuationTaskId"] = child_task_id
    return updated


def _find_candidate_tasks(group: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state = group.get("continuation") if isinstance(group.get("continuation"), dict) else {}
    linked_ids = {state.get("childTaskId"), group.get("continuationRunId"), group.get("continuationTaskId")}
    return [
        task for task in tasks
        if (
            isinstance(task.get("metadata"), dict)
            and task["metadata"].get("parent_continuation") is True
            and task["metadata"].get("launch_group_id") == group.get("id")
        ) or task.get("id") in linked_ids
    ]


def _is_legacy_claim(group: dict[str, Any]) -> bool:
    state = group.get("continuation") if isinstance(group.get("continuation"), dict) else None
    if state is None:
        return bool(group.get("continuationRequestedAt"))
    if state.get("phase") not in {"claimed", "starting"}:
        return False
    return not state.get("claimToken") or not state.get("leaseExpiresAt")


def _is_expired_claim(state: dict[str, Any] | None, now: datetime) -> bool:
    expires = _try_parse_time((state or {}).get("leaseExpiresAt"))
    return bool(state and state.get("phase") in {"claimed", "starting"} and expires and expires <= now)


async def _inspect_legacy_claimants(groups: list[dict[str, Any]]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for group in groups:
        state = group.get("continuation") if isinstance(group.get("continuation"), dict) else None
        if not _is_legacy_claim(group) or state is None:
            statuses[str(group.get("id"))] = "unknown"
            continue
        statuses[str(group.get("id"))] = await verifyProcessBirthMarker({
            "schemaVersion": 1,
            "pid": state.get("claimantPid"),
            "startToken": state.get("claimantStartToken"),
            "platform": state.get("claimantPlatform"),
        })
    return statuses


def _first_timestamp(*values: Any) -> datetime | None:
    for value in values:
        parsed = _try_parse_time(value)
        if parsed:
            return parsed
    return None


recover_subagent_launch_group_continuations = recoverSubagentLaunchGroupContinuations
