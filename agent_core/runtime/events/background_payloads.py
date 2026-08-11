"""Typed payloads for background-work events: daemon, tasks, monitors,
collaboration, remote sessions, and bridges."""
from __future__ import annotations

from typing import Any

from .shared import (
    _array_field,
    _collaboration_worker_payload,
    _field,
    _number_field,
    _optional_boolean,
    _optional_number,
    _optional_string,
    _string_array,
    _string_field,
    _task_lifecycle_payload,
    _task_snapshot_payload,
)

def background_typed_payload(event: str, data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if event == "daemon.snapshot":
        return "daemon.snapshot", {
            "phase": _optional_string(_field(data, "phase")),
            "epoch": _optional_string(_field(data, "epoch")),
            "pid": _optional_number(_field(data, "pid")),
            "socketPath": _optional_string(_field(data, "socket_path", "socketPath")),
            "supervisorId": _optional_string(_field(data, "supervisor_id", "supervisorId")),
            "attachedClients": _optional_number(_field(data, "attached_clients", "attachedClients")),
            "watchedSessions": _optional_number(_field(data, "watched_sessions", "watchedSessions")),
            "activeTasks": _optional_number(_field(data, "active_tasks", "activeTasks")),
            "updatedAt": _optional_string(_field(data, "updated_at", "updatedAt")),
        }
    if event == "daemon.recovering":
        return "daemon.recovering", {
            "phase": _optional_string(_field(data, "phase")),
            "epoch": _optional_string(_field(data, "epoch")),
        }
    if event == "daemon.recovered":
        return "daemon.recovered", {
            "phase": _optional_string(_field(data, "phase")),
            "epoch": _optional_string(_field(data, "epoch")),
        }
    if event == "task.snapshot":
        return "task.snapshot", {
            "total": _number_field(_field(data, "total")),
            "active": _number_field(_field(data, "active")),
            "backgrounded": _number_field(_field(data, "backgrounded")),
            "awaitingInput": _number_field(_field(data, "awaiting_input", "awaitingInput")),
            "failed": _number_field(_field(data, "failed")),
            "labels": _string_array(_field(data, "labels")),
            "tasks": [_task_snapshot_payload(task) for task in _array_field(_field(data, "tasks"))],
        }
    if event in {"task.registered", "task.updated", "task.restored"}:
        return event, _task_lifecycle_payload(data, include_status=True)
    if event in {"task.completed", "task.failed"}:
        return event, _task_lifecycle_payload(data, include_status=False)
    if event == "task.output_delta":
        return "task.output_delta", {
            "taskId": _string_field(_field(data, "task_id", "taskId")),
            "bytesDelta": _optional_number(_field(data, "bytes_delta", "bytesDelta")),
            "outputCursor": _optional_number(_field(data, "output_cursor", "outputCursor")),
        }
    if event == "health_check.unhealthy":
        return "health_check.unhealthy", {
            "taskId": _string_field(_field(data, "task_id", "taskId")),
            "title": _optional_string(_field(data, "title")),
            "failures": _optional_number(_field(data, "failures")),
            "error": _optional_string(_field(data, "error")),
        }
    if event == "health_check.recovered":
        return "health_check.recovered", {
            "taskId": _string_field(_field(data, "task_id", "taskId")),
            "title": _optional_string(_field(data, "title")),
        }
    if event == "monitor.snapshot":
        return "monitor.snapshot", {
            "total": _number_field(_field(data, "total")),
            "active": _number_field(_field(data, "active")),
            "failed": _number_field(_field(data, "failed")),
            "stopped": _number_field(_field(data, "stopped")),
            "muted": _number_field(_field(data, "muted")),
            "nextCheckInMs": _optional_number(_field(data, "next_check_in_ms", "nextCheckInMs")),
            "labels": _string_array(_field(data, "labels")),
        }
    if event == "monitor.created":
        return "monitor.created", {
            "monitorId": _optional_string(_field(data, "monitor_id", "monitorId")),
            "taskId": _optional_string(_field(data, "task_id", "taskId")),
            "status": _optional_string(_field(data, "status")),
            "goal": _optional_string(_field(data, "goal")),
            "changed": _optional_boolean(_field(data, "changed")),
            "revision": _optional_number(_field(data, "revision")),
            "transitionId": _optional_string(
                _field(data, "transition_id", "transitionId")
            ),
        }
    if event == "monitor.assistant_update":
        return "monitor.assistant_update", {
            "monitorId": _optional_string(_field(data, "monitor_id", "monitorId")),
            "taskId": _optional_string(_field(data, "task_id", "taskId")),
            "status": _optional_string(_field(data, "status")),
            "reason": _optional_string(_field(data, "reason")),
            "text": _field(data, "text") or "",
        }
    if event == "monitor.status_changed":
        return "monitor.status_changed", {
            "monitorId": _optional_string(_field(data, "monitor_id", "monitorId")),
            "taskId": _optional_string(_field(data, "task_id", "taskId")),
            "status": _optional_string(_field(data, "status")),
            "taskStatus": _optional_string(_field(data, "task_status", "taskStatus")),
            "exitCode": _optional_number(_field(data, "exit_code", "exitCode")),
            "previousStatus": _optional_string(
                _field(data, "previous_status", "previousStatus")
            ),
            "changed": _optional_boolean(_field(data, "changed")),
            "revision": _optional_number(_field(data, "revision")),
            "transitionId": _optional_string(
                _field(data, "transition_id", "transitionId")
            ),
        }
    if event == "collaboration.snapshot":
        return "collaboration.snapshot", {
            "activeCount": _number_field(_field(data, "active_count", "activeCount")),
            "awaitingInputCount": _number_field(_field(data, "awaiting_input_count", "awaitingInputCount")),
            "backgroundCount": _number_field(_field(data, "background_count", "backgroundCount")),
            "remoteStatus": _optional_string(_field(data, "remote_status", "remoteStatus")),
            "remoteTitle": _optional_string(_field(data, "remote_title", "remoteTitle")),
            "bridgeStatus": _optional_string(_field(data, "bridge_status", "bridgeStatus")),
            "pendingBridgeMessages": _optional_number(_field(data, "pending_bridge_messages", "pendingBridgeMessages")),
            "labels": _string_array(_field(data, "labels")),
            "workers": [_collaboration_worker_payload(worker) for worker in _array_field(_field(data, "workers"))],
        }
    if event == "collaboration.assistant_update":
        return "collaboration.assistant_update", {
            "sourceKind": _optional_string(_field(data, "source_kind", "sourceKind")),
            "sourceId": _optional_string(_field(data, "source_id", "sourceId")),
            "status": _optional_string(_field(data, "status")),
            "label": _optional_string(_field(data, "label")),
            "text": _field(data, "text") or "",
        }
    if event == "remote.status_changed":
        return "remote.status_changed", {
            "remoteSessionId": _optional_string(_field(data, "remote_session_id", "remoteSessionId")),
            "sessionId": _optional_string(_field(data, "session_id", "sessionId")),
            "status": _optional_string(_field(data, "status")),
            "title": _optional_string(_field(data, "title")),
            "bridgeId": _optional_string(_field(data, "bridge_id", "bridgeId")),
            "bridgeStatus": _optional_string(_field(data, "bridge_status", "bridgeStatus")),
            "unreadBridgeMessages": _optional_number(_field(data, "unread_bridge_messages", "unreadBridgeMessages")),
            "summary": _optional_string(_field(data, "summary")),
            "changeKind": _optional_string(_field(data, "change_kind", "changeKind")),
        }
    if event == "bridge.status_changed":
        return "bridge.status_changed", {
            "bridgeId": _optional_string(_field(data, "bridge_id", "bridgeId")),
            "remoteSessionId": _optional_string(_field(data, "remote_session_id", "remoteSessionId")),
            "status": _optional_string(_field(data, "status")),
            "pendingMessages": _optional_number(_field(data, "pending_messages", "pendingMessages")),
            "lastEventName": _optional_string(_field(data, "last_event_name", "lastEventName")),
            "summary": _optional_string(_field(data, "summary")),
            "changeKind": _optional_string(_field(data, "change_kind", "changeKind")),
        }
    if event == "bridge.message":
        return "bridge.message", {
            "bridgeId": _optional_string(_field(data, "bridge_id", "bridgeId")),
            "remoteSessionId": _optional_string(_field(data, "remote_session_id", "remoteSessionId")),
            "text": _optional_string(_field(data, "text")),
            "source": _optional_string(_field(data, "source")),
        }
    return None
