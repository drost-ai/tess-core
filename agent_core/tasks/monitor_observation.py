from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knob_value

    return knob_value(name)


from .._utils import parse_iso_ms

from datetime import datetime, timezone
from typing import Any
from .monitor_delivery_state import queue_observation




def apply_monitor_observation(
    monitor: dict[str, Any], *, task: dict[str, Any] | None, outputSize: int, now: int
) -> dict[str, Any]:
    observed_task_status = (task or {}).get("status") or "unknown"
    final_status = _derive_monitor_status(task, monitor)
    status_changed = final_status != monitor.get("status")
    task_transitioned = observed_task_status != monitor.get("lastObservedTaskStatus")
    now_iso = datetime.fromtimestamp(now / 1000, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    was_terminal = is_terminal_monitor_status(monitor.get("status"))
    became_terminal = not was_terminal and is_terminal_monitor_status(final_status)
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


def build_monitor_observation_mutation(
    latest: dict[str, Any],
    task: dict[str, Any] | None,
    outputSize: int,
    now: int,
    outputIdentity: str | None = None,
) -> dict[str, Any]:
    transition = apply_monitor_observation(
        latest, task=task, outputSize=outputSize, now=now
    )
    record = queue_observation(latest, transition["record"], task,
                               transition["reason"], now, outputIdentity)
    transition = {**transition, "record": record, "changed": record != latest}
    return {**transition, "value": transition}


def build_monitor_narration_mutation(
    latest: dict[str, Any],
    expectedRevision: int,
    updateTime: str,
    text: str,
    outputSize: int,
    reason: str,
) -> dict[str, Any]:
    if latest.get("revision") != expectedRevision or is_terminal_monitor_status(
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


def is_terminal_monitor_status(status: Any) -> bool:
    return status in {"completed", "failed", "stopped"}


def _derive_monitor_status(
    task: dict[str, Any] | None, monitor: dict[str, Any]
) -> str:
    if is_terminal_monitor_status(monitor.get("status")):
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
    if is_terminal_monitor_status(monitor.get("status")):
        return None
    if status_changed and final_status not in {"active", "muted"}:
        return final_status
    delivery = monitor.get("delivery") or {}
    if not monitor.get("lastAssistantUpdateAt") and not delivery.get("lastQueuedKey") and not delivery.get("lastProcessedAt"):
        return "start"
    progress = max(0, output_size - int(delivery.get("processedBytes", monitor.get("lastNarratedOutputSize")) or 0))
    if output_size != int(monitor.get("lastObservedOutputSize") or 0) and progress >= int(_knob("tasks.monitor_min_progress_bytes")):
        return "progress"
    if now - int(parse_iso_ms(delivery.get("lastProcessedAt") or monitor.get("lastAssistantUpdateAt") or monitor.get("createdAt")) or 0) >= int(monitor.get("heartbeatIntervalMs") or 0):
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
