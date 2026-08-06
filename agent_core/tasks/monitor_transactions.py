from __future__ import annotations

import json
import re
from typing import Any, Callable

from .._utils import maybe_await
from ..runtime.identifiers import parseMonitorId
from ..storage import (
    listSessionMonitors,
    loadMonitor,
    saveMonitor,
    withRecordTransaction,
)


class MonitorTransactionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def commitMonitorMutation(
    *,
    monitorId: str,
    expectedSessionId: str,
    mutate: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    monitor_id = parseMonitorId(monitorId)
    async with withRecordTransaction("monitor-record", monitor_id):
        previous = await loadMonitor(monitor_id)
        if not previous:
            raise MonitorTransactionError(
                "monitor.not_found", f"Monitor not found: {monitor_id}"
            )
        if previous.get("sessionId") != expectedSessionId:
            raise MonitorTransactionError(
                "monitor.not_owned",
                f"Monitor not found in the active session: {monitor_id}",
            )
        mutation = await maybe_await(mutate(previous))
        if not isinstance(mutation, dict) or not isinstance(mutation.get("record"), dict):
            raise TypeError("Monitor mutation must return a record and changed flag.")
        _assert_stable_identity(previous, mutation["record"])
        if not mutation.get("changed"):
            return {
                "previous": previous,
                "current": previous,
                "changed": False,
                "statusChanged": False,
                "revision": monitorRevision(previous),
                "value": mutation.get("value"),
            }
        revision = monitorRevision(previous) + 1
        status_changed = previous.get("status") != mutation["record"].get("status")
        event_bearing = status_changed or mutation.get("transition") is True
        transition_id = (
            _format_transition_id(
                monitor_id,
                revision,
                mutation.get("transitionLabel")
                or f"status:{previous.get('status')}->{mutation['record'].get('status')}",
            )
            if event_bearing
            else None
        )
        current = {
            **mutation["record"],
            "revision": revision,
            "lastTransitionId": transition_id or previous.get("lastTransitionId"),
        }
        if current.get("lastTransitionId") is None:
            current.pop("lastTransitionId", None)
        await saveMonitor(current)
        return {
            "previous": previous,
            "current": current,
            "changed": True,
            "statusChanged": status_changed,
            "revision": revision,
            "transitionId": transition_id,
            "value": mutation.get("value"),
        }


async def startMonitorOnce(
    *, sessionId: str, taskId: str, create: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    key = json.dumps([sessionId, taskId], separators=(",", ":"))
    async with withRecordTransaction("monitor-start", key):
        existing = next(
            (
                monitor
                for monitor in await listSessionMonitors(sessionId)
                if monitor.get("taskId") == taskId
                and monitor.get("status") in {"active", "muted"}
            ),
            None,
        )
        if existing:
            return {
                "monitor": existing,
                "changed": False,
                "revision": monitorRevision(existing),
            }
        created = create()
        if created.get("sessionId") != sessionId or created.get("taskId") != taskId:
            raise MonitorTransactionError(
                "monitor.identity_changed",
                "Monitor creation changed its session or task identity.",
            )
        revision = 1
        transition_id = _format_transition_id(
            created["id"], revision, "created->active"
        )
        monitor = {
            **created,
            "revision": revision,
            "lastTransitionId": transition_id,
        }
        await saveMonitor(monitor)
        return {
            "monitor": monitor,
            "changed": True,
            "revision": revision,
            "transitionId": transition_id,
        }


def monitorRevision(record: dict[str, Any]) -> int:
    value = record.get("revision")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def monitorTransitionEventFields(commit: dict[str, Any]) -> dict[str, Any]:
    result = {"revision": commit["revision"]}
    if commit.get("transitionId"):
        result["transition_id"] = commit["transitionId"]
    return result


def _assert_stable_identity(
    previous: dict[str, Any], current: dict[str, Any]
) -> None:
    if any(
        current.get(field) != previous.get(field)
        for field in ("id", "sessionId", "taskId")
    ):
        raise MonitorTransactionError(
            "monitor.identity_changed",
            f"Monitor mutation changed durable identity: {previous.get('id')}",
        )


def _format_transition_id(monitor_id: str, revision: int, label: str) -> str:
    normalized = re.sub(r"[^a-z0-9:._>\-]+", "_", str(label), flags=re.IGNORECASE)[:160]
    return f"monitor:{monitor_id}:r{revision}:{normalized}"


