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
            "cwd": _string_field(_field(data, "cwd")),
            "provider": _provider_field(_field(data, "provider")),
            "model": _string_field(_field(data, "model")),
            "permissionMode": _permission_mode_field(_field(data, "permission_mode", "permissionMode")),
            "workspaceName": _field(data, "workspace_name", "workspaceName"),
            "gitRoot": _field(data, "git_root", "gitRoot"),
            "gitBranch": _field(data, "git_branch", "gitBranch"),
            "remoteSessionId": _field(data, "remote_session_id", "remoteSessionId"),
            "bridgeId": _field(data, "bridge_id", "bridgeId"),
            "updatedAt": _field(data, "updated_at", "updatedAt"),
        }
    if event == "message.user":
        return "message.user", {"text": _string_field(_field(data, "text"))}
    if event == "message.assistant":
        return "message.assistant", {
            "text": _string_field(_field(data, "text")),
            "model": _optional_string(_field(data, "model")),
        }
    if event == "message.assistant.partial":
        return "message.assistant.partial", {
            "text": _optional_string(_field(data, "text")),
            "textDelta": _optional_string(_field(data, "text_delta", "textDelta")),
        }
    if event == "transcript.repaired":
        return "transcript.repaired", {
            "truncatedFrom": _optional_number(_field(data, "truncated_from", "truncatedFrom")),
            "removedCount": _optional_number(_field(data, "removed_count", "removedCount")),
            "missingToolUseIds": _string_array(_field(data, "missing_tool_use_ids", "missingToolUseIds")),
        }
    if event == "turn.started":
        return "turn.started", {
            "prompt": _string_field(_field(data, "prompt")),
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
        return "turn.failed", {"message": _string_field(_field(data, "message"))}
    if event == "turn.cancelled":
        return "turn.cancelled", {
            "model": _field(data, "model"),
            "partialText": _field(data, "partial_text", "partialText"),
            "stopReason": _field(data, "stop_reason", "stopReason"),
        }
    if event == "status.updated":
        return "status.updated", {"message": _string_field(_field(data, "message")), "detail": _field(data, "detail")}
    if event == "status.cleared":
        return "status.cleared", {}
    if event == "provider.retrying":
        return "provider.retrying", {
            "providerLabel": _optional_string(_field(data, "provider_label", "providerLabel")),
            "nextAttempt": _optional_number(_field(data, "next_attempt", "nextAttempt")),
            "delayMs": _optional_number(_field(data, "delay_ms", "delayMs")),
        }
    if event == "provider.recovered":
        return "provider.recovered", {
            "providerLabel": _optional_string(_field(data, "provider_label", "providerLabel")),
            "attempts": _optional_number(_field(data, "attempts")),
        }
    if event == "provider.failed":
        return "provider.failed", {
            "provider": _optional_string(_field(data, "provider")),
            "providerLabel": _optional_string(_field(data, "provider_label", "providerLabel")),
            "category": _optional_string(_field(data, "category")),
            "phase": _optional_string(_field(data, "phase")),
            "message": _optional_string(_field(data, "message")),
            "retryable": _optional_boolean(_field(data, "retryable")),
            "attempt": _optional_number(_field(data, "attempt")),
            "status": _optional_number(_field(data, "status")),
            "code": _optional_string(_field(data, "code")),
            "errorType": _optional_string(_field(data, "error_type", "errorType")),
        }
    if event == "provider.input_tokens":
        return "provider.input_tokens", {
            "provider": _optional_string(_field(data, "provider")),
            "model": _optional_string(_field(data, "model")),
            "inputTokens": _optional_number(_field(data, "input_tokens", "inputTokens")),
            "source": _optional_string(_field(data, "source")),
            "baseInputTokens": _optional_number(_field(data, "base_input_tokens", "baseInputTokens")),
            "safetyMarginTokens": _optional_number(_field(data, "safety_margin_tokens", "safetyMarginTokens")),
        }
    if event == "provider.fallback":
        return "provider.fallback", {
            "provider": _optional_string(_field(data, "provider")),
            "model": _optional_string(_field(data, "model")),
            "error": _optional_string(_field(data, "error")),
        }
    if event == "tool.started":
        return "tool.started", {"name": _string_field(_field(data, "name")), "input": _field(data, "input")}
    if event == "tool.completed":
        return "tool.completed", {
            "name": _string_field(_field(data, "name")),
            "output": _field(data, "output"),
            "error": _optional_boolean(_field(data, "error")),
        }
    if event == "permission.requested":
        return "permission.requested", {"summary": _optional_string(_field(data, "summary"))}
    if event == "permission.decided":
        return "permission.decided", {
            "decision": _optional_string(_field(data, "decision")),
            "summary": _optional_string(_field(data, "summary")),
        }
    if event == "plan.requested":
        return "plan.requested", {
            "plan": _optional_string(_field(data, "plan")),
            "filePath": _optional_string(_field(data, "file_path", "filePath")),
        }
    if event == "plan.approved":
        return "plan.approved", {"summary": _optional_string(_field(data, "summary"))}
    if event == "plan.rejected":
        return "plan.rejected", {"summary": _optional_string(_field(data, "summary"))}
    if event == "plan.contract_enforced":
        return "plan.contract_enforced", {"reason": _optional_string(_field(data, "reason"))}
    if event == "compaction.started":
        return "compaction.started", {
            "trigger": _optional_string(_field(data, "trigger")),
            "currentTokenEstimate": _optional_number(_field(data, "current_token_estimate", "currentTokenEstimate")),
            "currentTokenSource": _optional_string(_field(data, "current_token_source", "currentTokenSource")),
        }
    if event == "compaction.skipped":
        return "compaction.skipped", {
            "trigger": _optional_string(_field(data, "trigger")),
            "reason": _optional_string(_field(data, "reason")),
            "detail": _optional_string(_field(data, "detail")),
            "qualityClassification": _optional_string(_field(data, "quality_classification", "qualityClassification")),
        }
    if event == "compaction.completed":
        return "compaction.completed", {
            "generation": _optional_number(_field(data, "generation")),
            "trigger": _optional_string(_field(data, "trigger")),
            "compactedThroughMessageIndex": _optional_number(_field(data, "compacted_through_message_index", "compactedThroughMessageIndex")),
            "compactedMessageCount": _optional_number(_field(data, "compacted_message_count", "compactedMessageCount")),
            "recentMessageCount": _optional_number(_field(data, "recent_message_count", "recentMessageCount")),
            "rawTokenEstimate": _optional_number(_field(data, "raw_token_estimate", "rawTokenEstimate")),
            "workingContextTokenEstimate": _optional_number(_field(data, "working_context_token_estimate", "workingContextTokenEstimate")),
            "workingContextTokenSource": _optional_string(_field(data, "working_context_token_source", "workingContextTokenSource")),
            "qualityClassification": _optional_string(_field(data, "quality_classification", "qualityClassification")),
            "compactionProvider": _optional_string(_field(data, "compaction_provider", "compactionProvider")),
            "compactionModel": _optional_string(_field(data, "compaction_model", "compactionModel")),
            "verificationProvider": _optional_string(_field(data, "verification_provider", "verificationProvider")),
            "verificationModel": _optional_string(_field(data, "verification_model", "verificationModel")),
            "verificationVerdict": _optional_string(_field(data, "verification_verdict", "verificationVerdict")),
            "verificationConfidence": _optional_string(_field(data, "verification_confidence", "verificationConfidence")),
            "summaryPath": _optional_string(_field(data, "summary_path", "summaryPath")),
            "statePath": _optional_string(_field(data, "state_path", "statePath")),
            "workingContextPath": _optional_string(_field(data, "working_context_path", "workingContextPath")),
            "checkpointPath": _optional_string(_field(data, "checkpoint_path", "checkpointPath")),
        }
    if event == "compaction.failed":
        return "compaction.failed", {
            "trigger": _optional_string(_field(data, "trigger")),
            "error": _optional_string(_field(data, "error")),
            "provider": _optional_string(_field(data, "provider")),
            "providerLabel": _optional_string(_field(data, "provider_label", "providerLabel")),
            "category": _optional_string(_field(data, "category")),
            "phase": _optional_string(_field(data, "phase")),
            "retryable": _optional_boolean(_field(data, "retryable")),
        }
    if event == "context_relief.selected":
        return "context_relief.selected", {
            "mode": _context_relief_mode_field(_field(data, "mode")),
            "reason": _optional_string(_field(data, "reason")),
            "tokenEstimate": _optional_number(_field(data, "token_estimate", "tokenEstimate")),
            "pressureLevel": _optional_string(_field(data, "pressure_level", "pressureLevel")),
            "reactiveAttempt": _optional_number(_field(data, "reactive_attempt", "reactiveAttempt")),
            "plannedModes": _context_relief_mode_array(_field(data, "planned_modes", "plannedModes")),
        }
    if event == "context_relief.applied":
        return "context_relief.applied", {
            "mode": _context_relief_mode_field(_field(data, "mode")),
            "modes": _context_relief_mode_array(_field(data, "modes")),
            "tokensSavedEstimate": _optional_number(_field(data, "tokens_saved_estimate", "tokensSavedEstimate")),
            "droppedMessageCount": _optional_number(_field(data, "dropped_message_count", "droppedMessageCount")),
            "compactedMessageCount": _optional_number(_field(data, "compacted_message_count", "compactedMessageCount")),
            "detail": _optional_string(_field(data, "detail")),
            "pressureLevel": _optional_string(_field(data, "pressure_level", "pressureLevel")),
            "reactiveAttempt": _optional_number(_field(data, "reactive_attempt", "reactiveAttempt")),
            "reinjectionSummary": _optional_string(_field(data, "reinjection_summary", "reinjectionSummary")),
            "reinjectionSectionCount": _optional_number(_field(data, "reinjection_section_count", "reinjectionSectionCount")),
        }
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
            "bridgeId": _optional_string(_field(data, "bridge_id", "bridgeId")),
            "remoteSessionId": _optional_string(_field(data, "remote_session_id", "remoteSessionId")),
            "text": _optional_string(_field(data, "text")),
            "source": _optional_string(_field(data, "source")),
        }
    if event in KNOWN_RUNTIME_EVENTS:
        return event, payload
    if event.startswith("bridge."):
        return "bridge.event", {
            "eventName": event,
            "bridgeId": _optional_string(_field(data, "bridge_id", "bridgeId")),
            "remoteSessionId": _optional_string(_field(data, "remote_session_id", "remoteSessionId")),
        }
    if event.startswith("remote."):
        return "remote.event", {
            "eventName": event,
            "remoteSessionId": _optional_string(_field(data, "remote_session_id", "remoteSessionId")),
        }
    if event.startswith("mcp."):
        return "mcp.event", {
            "eventName": event,
            "channelName": _optional_string(_field(data, "channel_name", "channelName")),
        }
    if event.startswith("platform."):
        return "platform.event", {"eventName": event}
    if event.startswith("subagent."):
        return "subagent.event", {"eventName": event}
    if event == "budget.exceeded":
        return "budget.exceeded", {
            "estimatedCostUsd": _optional_number(_field(data, "estimated_cost_usd", "estimatedCostUsd")),
            "maxBudgetUsd": _optional_number(_field(data, "max_budget_usd", "maxBudgetUsd")),
            "model": _optional_string(_field(data, "model")),
        }
    return "runtime.unknown", {"eventName": event}


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
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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


_CONTEXT_RELIEF_MODES = {
    "microcompact",
    "api_microcompact",
    "time_based_microcompact",
    "partial_compaction",
    "session_memory_compaction",
    "full_checkpoint_compaction",
}


def _context_relief_mode_field(value: Any) -> str:
    return value if value in _CONTEXT_RELIEF_MODES else "none"


def _context_relief_mode_array(value: Any) -> list[str] | None:
    return [_context_relief_mode_field(item) for item in value] if isinstance(value, list) else None


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
