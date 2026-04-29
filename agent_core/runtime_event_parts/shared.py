from __future__ import annotations

import math
from typing import Any

from .._utils import now_iso


KNOWN_RUNTIME_EVENTS = {
    "session.activated",
    "message.user",
    "message.assistant",
    "message.assistant.partial",
    "transcript.repaired",
    "turn.started",
    "turn.cancelling",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "status.updated",
    "status.cleared",
    "provider.retrying",
    "provider.recovered",
    "provider.failed",
    "provider.input_tokens",
    "provider.fallback",
    "tool.started",
    "tool.completed",
    "permission.requested",
    "permission.decided",
    "plan.requested",
    "plan.approved",
    "plan.rejected",
    "plan.contract_enforced",
    "compaction.started",
    "compaction.skipped",
    "compaction.completed",
    "compaction.failed",
    "context_relief.selected",
    "context_relief.applied",
    "daemon.snapshot",
    "daemon.recovering",
    "daemon.recovered",
    "task.snapshot",
    "task.registered",
    "task.updated",
    "task.output_delta",
    "task.completed",
    "task.failed",
    "task.restored",
    "health_check.unhealthy",
    "health_check.recovered",
    "monitor.snapshot",
    "monitor.created",
    "monitor.assistant_update",
    "monitor.status_changed",
    "collaboration.snapshot",
    "collaboration.assistant_update",
    "remote.status_changed",
    "bridge.status_changed",
    "bridge.message",
}



_CONTEXT_RELIEF_MODES = {
    "microcompact",
    "api_microcompact",
    "time_based_microcompact",
    "partial_compaction",
    "session_memory_compaction",
    "full_checkpoint_compaction",
}

_TASK_KINDS = {"background_command", "monitor", "health_check", "main_session", "subagent", "remote"}
_TASK_STATUSES = {
    "queued",
    "running",
    "backgrounded",
    "awaiting_input",
    "reconciling",
    "degraded",
    "completed",
    "failed",
    "stopped",
    "idle",
}

def _field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data.get(name)
    return None

def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None

def _string_field(value: Any) -> str:
    return value if isinstance(value, str) else ""

def _provider_field(value: Any) -> str:
    return "anthropic" if value == "anthropic" else "openai"

def _permission_mode_field(value: Any) -> str:
    return value if value in {"acceptEdits", "bypassPermissions", "default", "dontAsk", "plan", "auto"} else "default"

def _optional_number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None

def _optional_boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None

def _number_field(value: Any) -> int | float:
    number = _optional_number(value)
    return number if number is not None else 0

def _string_array(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []

def _array_field(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []

def _record_field(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}

def _context_relief_mode_field(value: Any) -> str:
    return value if value in _CONTEXT_RELIEF_MODES else "none"

def _context_relief_mode_array(value: Any) -> list[str] | None:
    return [_context_relief_mode_field(item) for item in value] if isinstance(value, list) else None

def _optional_task_kind(value: Any) -> str | None:
    return value if value in _TASK_KINDS else None

def _optional_task_status(value: Any) -> str | None:
    return value if value in _TASK_STATUSES else None

def _optional_foreground_mode(value: Any) -> str | None:
    return value if value in {"foreground", "background"} else None

def _task_id_field(data: dict[str, Any]) -> str:
    task_id = _field(data, "task_id", "taskId")
    return task_id if isinstance(task_id, str) else ""

def _task_lifecycle_payload(data: dict[str, Any], *, include_status: bool) -> dict[str, Any]:
    payload = {
        "taskId": _task_id_field(data),
        "kind": _optional_task_kind(_field(data, "kind")),
        "title": _optional_string(_field(data, "title")),
        "summary": _optional_string(_field(data, "summary")),
        "foregroundMode": _optional_foreground_mode(_field(data, "foreground_mode", "foregroundMode")),
        "taskFamily": _optional_string(_field(data, "task_family", "taskFamily")),
        "detailStatus": _optional_string(_field(data, "detail_status", "detailStatus")),
        "attachableSessionId": _optional_string(_field(data, "attachable_session_id", "attachableSessionId")),
        "retrieveOnly": _optional_boolean(_field(data, "retrieve_only", "retrieveOnly")),
        "resultSummary": _optional_string(_field(data, "result_summary", "resultSummary")),
    }
    if include_status:
        payload = {**payload, "status": _optional_task_status(_field(data, "status"))}
    return payload

def _task_snapshot_payload(value: Any) -> dict[str, Any]:
    task = _record_field(value)
    return {
        "id": str(_field(task, "id") or ""),
        "kind": _optional_task_kind(_field(task, "kind")),
        "status": _optional_task_status(_field(task, "status")),
        "title": _optional_string(_field(task, "title")),
        "summary": _optional_string(_field(task, "summary")),
        "updatedAt": _optional_string(_field(task, "updated_at", "updatedAt")),
        "sessionId": _optional_string(_field(task, "session_id", "sessionId")),
        "foregroundMode": _optional_foreground_mode(_field(task, "foreground_mode", "foregroundMode")),
        "taskFamily": _optional_string(_field(task, "task_family", "taskFamily")),
        "detailStatus": _optional_string(_field(task, "detail_status", "detailStatus")),
        "attachableSessionId": _optional_string(_field(task, "attachable_session_id", "attachableSessionId")),
        "retrieveOnly": _optional_boolean(_field(task, "retrieve_only", "retrieveOnly")),
        "resultSummary": _optional_string(_field(task, "result_summary", "resultSummary")),
    }

def _collaboration_worker_payload(value: Any) -> dict[str, Any]:
    worker = _record_field(value)
    return {
        "id": str(_field(worker, "id") or ""),
        "kind": _optional_string(_field(worker, "kind")),
        "status": _optional_string(_field(worker, "status")),
        "label": _optional_string(_field(worker, "label")),
        "summary": _optional_string(_field(worker, "summary")),
        "sessionId": _optional_string(_field(worker, "session_id", "sessionId")),
        "parentSessionId": _optional_string(_field(worker, "parent_session_id", "parentSessionId")),
        "parentTaskId": _optional_string(_field(worker, "parent_task_id", "parentTaskId")),
        "childSessionId": _optional_string(_field(worker, "child_session_id", "childSessionId")),
        "taskId": _optional_string(_field(worker, "task_id", "taskId")),
        "monitorId": _optional_string(_field(worker, "monitor_id", "monitorId")),
        "remoteSessionId": _optional_string(_field(worker, "remote_session_id", "remoteSessionId")),
        "bridgeId": _optional_string(_field(worker, "bridge_id", "bridgeId")),
        "attachableSessionId": _optional_string(_field(worker, "attachable_session_id", "attachableSessionId")),
        "owner": _optional_string(_field(worker, "owner")),
        "waitingReason": _optional_string(_field(worker, "waiting_reason", "waitingReason")),
        "completionReason": _optional_string(_field(worker, "completion_reason", "completionReason")),
        "resultSummary": _optional_string(_field(worker, "result_summary", "resultSummary")),
        "latestResultAt": _optional_string(_field(worker, "latest_result_at", "latestResultAt")),
        "retryable": _field(worker, "retryable") if isinstance(_field(worker, "retryable"), bool) else None,
        "stoppable": _field(worker, "stoppable") if isinstance(_field(worker, "stoppable"), bool) else None,
        "recoveryState": _optional_string(_field(worker, "recovery_state", "recoveryState")),
        "updatedAt": _optional_string(_field(worker, "updated_at", "updatedAt")),
    }

def _camelize_record(value: Any) -> Any:
    if isinstance(value, list):
        return [_camelize_record(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {_snake_to_camel(str(key)): _camelize_record(item) for key, item in value.items()}

def _snake_to_camel(value: str) -> str:
    if "_" not in value:
        return value
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
