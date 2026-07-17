from __future__ import annotations

from typing import Any


def stopMonitorRecord(monitor: dict[str, Any], now: str) -> dict[str, Any]:
    status = monitor.get("status")
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


def monitorStopNote(transition: dict[str, Any]) -> str | None:
    if transition.get("reason") == "already_stopped":
        return "Monitor was already stopped; no action was needed."
    if transition.get("reason") == "already_terminal":
        return f"Monitor already {transition['record'].get('status')}; no stop was needed."
    return None


def resumeMonitorRecord(monitor: dict[str, Any], now: str) -> dict[str, Any]:
    status = monitor.get("status")
    if status in {"completed", "failed"}:
        return {"record": monitor, "changed": False, "previousStatus": status, "reason": "already_terminal"}
    if status == "active":
        return {"record": monitor, "changed": False, "previousStatus": status, "reason": "already_active"}
    record = {**monitor, "status": "active", "updatedAt": now}
    record.pop("stoppedAt", None)
    return {"record": record, "changed": True, "previousStatus": status, "reason": "resumed"}


def monitorResumeNote(transition: dict[str, Any]) -> str | None:
    if transition.get("reason") == "already_active":
        return "Monitor was already active; no action was needed."
    if transition.get("reason") == "already_terminal":
        return f"Monitor already {transition['record'].get('status')}; it cannot be resumed."
    return None
