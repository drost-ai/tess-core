from __future__ import annotations

from typing import Any


def resolveTaskSemanticStatus(*statuses: Any) -> str:
    for status in statuses:
        normalized = str(status or "")
        if normalized == "completed":
            return "completed"
        if normalized == "failed":
            return "failed"
        if normalized in {"stopped", "terminated", "cancelled"}:
            return "stopped"
    return "running"


def applyDeliberateTaskStop(current: str) -> dict[str, Any]:
    if current != "running":
        return _terminal_noop(current)
    return {
        "previousStatus": "running",
        "status": "stopped",
        "changed": True,
        "event": "task.stopped",
        "summary": "Task stopped by deliberate request.",
    }


def applyTaskProcessCompletion(
    *,
    current: str,
    code: int | None,
    signalName: str | None,
    terminationIntent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if current != "running":
        return _terminal_noop(current)
    if signalName:
        expected_signal = (terminationIntent or {}).get("signal")
        expected = expected_signal == signalName or (
            expected_signal == "SIGTERM" and signalName == "SIGKILL"
        )
        return {
            "previousStatus": "running",
            "status": "stopped" if expected else "failed",
            "changed": True,
            "event": "task.stopped" if expected else "task.failed",
            "summary": (
                f"Task stopped after expected {signalName}."
                if expected
                else f"Task failed after unexpected {signalName}."
            ),
        }
    if code == 0:
        return {
            "previousStatus": "running",
            "status": "completed",
            "changed": True,
            "event": "task.completed",
            "summary": "Task completed successfully.",
        }
    return {
        "previousStatus": "running",
        "status": "failed",
        "changed": True,
        "event": "task.failed",
        "summary": f"Task failed{' with exit code ' + str(code) if isinstance(code, int) else ''}.",
    }


def toTaskStopResult(
    taskId: str,
    transition: dict[str, Any],
    transitionId: str | None = None,
) -> dict[str, Any]:
    changed = bool(transition.get("changed"))
    note = (
        str(transition.get("summary") or "")
        if changed
        else f"Task was already {transition.get('status')}; no process signal was sent."
    )
    result = {
        "taskId": taskId,
        "previousStatus": transition.get("previousStatus"),
        "status": transition.get("status"),
        "changed": changed,
        "note": _bound_note(note),
    }
    if changed and transitionId:
        result["transitionId"] = transitionId
    return result


def taskEventNameForStatus(status: str) -> str:
    if status == "completed":
        return "task.completed"
    if status == "failed":
        return "task.failed"
    if status == "stopped":
        return "task.stopped"
    return "task.updated"


def _terminal_noop(current: str) -> dict[str, Any]:
    return {
        "previousStatus": current,
        "status": current,
        "changed": False,
        "summary": f"Task was already {current}.",
    }


def _bound_note(value: str) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= 512:
        return value
    return raw[:509].decode("utf-8", errors="ignore") + "..."


resolve_task_semantic_status = resolveTaskSemanticStatus
apply_deliberate_task_stop = applyDeliberateTaskStop
apply_task_process_completion = applyTaskProcessCompletion
to_task_stop_result = toTaskStopResult
