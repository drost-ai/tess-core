from __future__ import annotations

from typing import Any
from copy import deepcopy


def stop_monitor_record(monitor: dict[str, Any], now: str) -> dict[str, Any]:
    status = monitor.get("status")
    if (monitor.get("delivery") and not monitor.get("deliveryStoppedAt")
            and (status not in {"completed", "failed", "stopped"} or monitor["delivery"].get("queue"))):
        record = deepcopy(monitor)
        record["deliveryStoppedAt"] = now
        record["delivery"]["queue"] = []
        record["delivery"]["lastError"] = "monitor.cancelled"
        record.update(updatedAt=now, stoppedAt=now)
        if status not in {"completed", "failed"}:
            record["status"] = "stopped"
        return {"record": record, "changed": True, "previousStatus": status, "reason": "stopped"}
    if status in {"completed", "failed"}:
        return {"record": monitor, "changed": False, "previousStatus": status, "reason": "already_terminal"}
    if status == "stopped":
        return {"record": monitor, "changed": False, "previousStatus": status, "reason": "already_stopped"}
    return {
        "record": {**monitor, "status": "stopped", "updatedAt": now, "stoppedAt": now},
        "changed": True,
        "previousStatus": status,
        "reason": "stopped",
    }


def monitor_stop_note(transition: dict[str, Any]) -> str | None:
    if transition.get("reason") == "already_stopped":
        return "Monitor was already stopped; no action was needed."
    if transition.get("reason") == "already_terminal":
        return f"Monitor already {transition['record'].get('status')}; no stop was needed."
    return None


def resume_monitor_record(monitor: dict[str, Any], now: str) -> dict[str, Any]:
    status = monitor.get("status")
    if status in {"completed", "failed"}:
        return {"record": monitor, "changed": False, "previousStatus": status, "reason": "already_terminal"}
    if status == "active":
        return {"record": monitor, "changed": False, "previousStatus": status, "reason": "already_active"}
    record = {**monitor, "status": "active", "updatedAt": now}
    record.pop("stoppedAt", None)
    record.pop("deliveryStoppedAt", None)
    if record.get("delivery"):
        record["delivery"] = {**record["delivery"], "queue": []}
        record["delivery"].pop("lastQueuedKey", None)
    return {"record": record, "changed": True, "previousStatus": status, "reason": "resumed"}


def monitor_resume_note(transition: dict[str, Any]) -> str | None:
    if transition.get("reason") == "already_active":
        return "Monitor was already active; no action was needed."
    if transition.get("reason") == "already_terminal":
        return f"Monitor already {transition['record'].get('status')}; it cannot be resumed."
    return None
