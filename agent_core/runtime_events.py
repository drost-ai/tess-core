from __future__ import annotations

from typing import Any

from ._utils import now_iso


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
    "bridge.message",
}


def createRuntimeEvent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    legacy_event = args.get("event") or args.get("type") or args.get("kind")
    data = args.get("data") or args.get("payload") or {}
    timestamp = args.get("timestamp") or args.get("createdAt") or now_iso()
    session_id = _field(data, "session_id", "sessionId") or args.get("defaultSessionId")
    kind, payload = _typed_payload(str(legacy_event or ""), data)
    return {
        "event": legacy_event,
        "type": legacy_event,
        "kind": kind,
        "data": data,
        "payload": payload,
        "createdAt": timestamp,
        "timestamp": timestamp,
        "sessionId": session_id,
        "legacyEvent": legacy_event,
        "legacyData": data,
    }


def _typed_payload(event: str, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    payload = _camelize_record(data)
    if event == "session.activated":
        return "session.activated", {
            "cwd": _field(data, "cwd"),
            "provider": _field(data, "provider"),
            "model": _field(data, "model"),
            "permissionMode": _field(data, "permission_mode", "permissionMode") or "default",
            "workspaceName": _field(data, "workspace_name", "workspaceName"),
            "gitRoot": _field(data, "git_root", "gitRoot"),
            "gitBranch": _field(data, "git_branch", "gitBranch"),
            "remoteSessionId": _field(data, "remote_session_id", "remoteSessionId"),
            "bridgeId": _field(data, "bridge_id", "bridgeId"),
            "updatedAt": _field(data, "updated_at", "updatedAt"),
        }
    if event == "turn.started":
        return "turn.started", {
            "prompt": _field(data, "prompt") or "",
            "model": _field(data, "model"),
            "agentName": _field(data, "agent_name", "agentName"),
        }
    if event == "turn.cancelling":
        return "turn.cancelling", {}
    if event == "turn.completed":
        return "turn.completed", {
            "stopReason": _field(data, "stop_reason", "stopReason"),
            "message": _field(data, "message"),
            "model": _field(data, "model"),
        }
    if event == "turn.failed":
        return "turn.failed", {"message": _field(data, "message") or ""}
    if event == "turn.cancelled":
        return "turn.cancelled", {
            "model": _field(data, "model"),
            "partialText": _field(data, "partial_text", "partialText"),
            "stopReason": _field(data, "stop_reason", "stopReason"),
        }
    if event == "status.updated":
        return "status.updated", {"message": _field(data, "message") or "", "detail": _field(data, "detail")}
    if event == "status.cleared":
        return "status.cleared", {}
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
    if event == "bridge.message":
        return "bridge.message", {
            "bridgeId": _field(data, "bridge_id", "bridgeId"),
            "remoteSessionId": _field(data, "remote_session_id", "remoteSessionId"),
            "text": _field(data, "text"),
            "source": _field(data, "source"),
        }
    if event in KNOWN_RUNTIME_EVENTS:
        return event, payload
    if event.startswith("bridge."):
        return "bridge.event", {"eventName": event, "bridgeId": _field(data, "bridge_id", "bridgeId"), "remoteSessionId": _field(data, "remote_session_id", "remoteSessionId")}
    if event.startswith("remote."):
        return "remote.event", {"eventName": event, "remoteSessionId": _field(data, "remote_session_id", "remoteSessionId")}
    if event.startswith("mcp."):
        return "mcp.event", {"eventName": event, "channelName": _field(data, "channel_name", "channelName")}
    if event.startswith("platform."):
        return "platform.event", {"eventName": event}
    if event.startswith("subagent."):
        return "subagent.event", {"eventName": event}
    if event == "budget.exceeded":
        return "budget.exceeded", payload
    return "runtime.unknown", {"eventName": event}


def _field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data.get(name)
    return None


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _number_field(value: Any) -> int | float:
    number = _optional_number(value)
    return number if number is not None else 0


def _string_array(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _array_field(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _record_field(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


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
