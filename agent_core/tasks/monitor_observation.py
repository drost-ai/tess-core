from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


MIN_PROGRESS_BYTES = 120


def applyMonitorObservation(
    monitor: dict[str, Any], *, task: dict[str, Any] | None, outputSize: int, now: int
) -> dict[str, Any]:
    observed_task_status = (task or {}).get("status") or "unknown"
    final_status = _derive_monitor_status(task, monitor)
    status_changed = final_status != monitor.get("status")
    task_transitioned = observed_task_status != monitor.get("lastObservedTaskStatus")
    now_iso = datetime.fromtimestamp(now / 1000, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    was_terminal = isTerminalMonitorStatus(monitor.get("status"))
    became_terminal = not was_terminal and isTerminalMonitorStatus(final_status)
    record = {
        **monitor,
        "updatedAt": now_iso,
        "status": final_status,
        "lastCheckAt": now_iso,
        "lastObservedTaskStatus": observed_task_status,
        "lastObservedOutputSize": outputSize,
    }
    if became_terminal:
        record["completedAt"] = monitor.get("completedAt") or now_iso
        if final_status in {"failed", "stopped"}:
            failure = (task or {}).get("error") or monitor.get("failureReason")
            if failure is not None:
                record["failureReason"] = failure
    return {
        "record": record,
        "changed": _observation_changed(monitor, record),
        "transition": status_changed or (not was_terminal and task_transitioned),
        "transitionLabel": (
            f"observation:{monitor.get('lastObservedTaskStatus') or 'none'}"
            f"->{observed_task_status}:status:{monitor.get('status')}->{final_status}"
        ),
        "taskTransitioned": task_transitioned,
        "reason": _narration_reason(
            monitor, final_status, status_changed, outputSize, now
        ),
    }


def buildMonitorObservationMutation(
    latest: dict[str, Any],
    task: dict[str, Any] | None,
    outputSize: int,
    now: int,
) -> dict[str, Any]:
    transition = applyMonitorObservation(
        latest, task=task, outputSize=outputSize, now=now
    )
    return {**transition, "value": transition}


def buildMonitorNarrationMutation(
    latest: dict[str, Any],
    expectedRevision: int,
    updateTime: str,
    text: str,
    outputSize: int,
    reason: str,
) -> dict[str, Any]:
    if latest.get("revision") != expectedRevision or isTerminalMonitorStatus(
        latest.get("status")
    ):
        return {
            "record": latest,
            "changed": False,
            "value": {"accepted": False},
        }
    record = {
        **latest,
        "lastAssistantUpdateAt": updateTime,
        "lastAssistantUpdateSummary": text,
        "lastNarratedOutputSize": outputSize,
    }
    if reason == "heartbeat":
        record["lastHeartbeatAt"] = updateTime
    return {
        "record": record,
        "changed": True,
        "transition": False,
        "value": {"accepted": True},
    }


def isTerminalMonitorStatus(status: Any) -> bool:
    return status in {"completed", "failed", "stopped"}


def _derive_monitor_status(
    task: dict[str, Any] | None, monitor: dict[str, Any]
) -> str:
    if isTerminalMonitorStatus(monitor.get("status")) or monitor.get("status") == "muted":
        return str(monitor.get("status"))
    task_status = (task or {}).get("status") or monitor.get("lastObservedTaskStatus") or "unknown"
    if task_status == "completed":
        return "completed"
    if task_status in {"failed", "unknown"}:
        return "failed"
    if task_status in {"terminated", "stopped"}:
        return "stopped"
    return "active"


def _narration_reason(
    monitor: dict[str, Any],
    final_status: str,
    status_changed: bool,
    output_size: int,
    now: int,
) -> str | None:
    if isTerminalMonitorStatus(monitor.get("status")):
        return None
    if not monitor.get("lastAssistantUpdateAt"):
        return "start"
    if status_changed and final_status not in {"active", "muted"}:
        return final_status
    progress = max(0, output_size - int(monitor.get("lastNarratedOutputSize") or 0))
    if output_size != int(monitor.get("lastObservedOutputSize") or 0) and progress >= MIN_PROGRESS_BYTES:
        return "progress"
    if now - _parse_iso_ms(monitor.get("lastAssistantUpdateAt") or monitor.get("createdAt")) >= int(monitor.get("heartbeatIntervalMs") or 0):
        return "heartbeat"
    return None


def _observation_changed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    keys = (
        "updatedAt",
        "status",
        "completedAt",
        "failureReason",
        "lastCheckAt",
        "lastObservedTaskStatus",
        "lastObservedOutputSize",
    )
    return any(previous.get(key) != current.get(key) for key in keys)


def _parse_iso_ms(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    try:
        return round(
            datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        )
    except ValueError:
        return 0


apply_monitor_observation = applyMonitorObservation
