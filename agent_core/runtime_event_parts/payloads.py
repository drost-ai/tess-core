from __future__ import annotations
from .shared import *
from .prompt_cache import promptCacheEventPayload
from .provider_payloads import providerEventPayload
def _typed_payload(event: str, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    provider_payload = providerEventPayload(event, data)
    if provider_payload is not None:
        return provider_payload
    payload = _camelize_record(data)
    if event == "session.activated":
        return "session.activated", {
            "cwd": _string_field(_field(data, "cwd")),
            "provider": _provider_field(_field(data, "provider")),
            "model": _string_field(_field(data, "model")),
            "permissionMode": _permission_mode_field(_field(data, "permission_mode", "permissionMode")),
            "workspaceName": _optional_string(_field(data, "workspace_name", "workspaceName")),
            "gitRoot": _optional_string(_field(data, "git_root", "gitRoot")),
            "gitBranch": _optional_string(_field(data, "git_branch", "gitBranch")),
            "remoteSessionId": _optional_string(_field(data, "remote_session_id", "remoteSessionId")),
            "bridgeId": _optional_string(_field(data, "bridge_id", "bridgeId")),
            "updatedAt": _optional_string(_field(data, "updated_at", "updatedAt")),
        }
    if event == "message.user":
        return "message.user", {"text": _string_field(_field(data, "text"))}
    if event == "message.assistant":
        return "message.assistant", {
            "text": _string_field(_field(data, "text")),
            "model": _optional_string(_field(data, "model")),
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
            "model": _optional_string(_field(data, "model")),
            "agentName": _optional_string(_field(data, "agent_name", "agentName")),
        }
    if event == "turn.cancelling":
        return "turn.cancelling", {}
    if event == "turn.completed":
        return "turn.completed", {
            "stopReason": _optional_string(_field(data, "stop_reason", "stopReason")),
            "message": _optional_string(_field(data, "message")),
            "model": _optional_string(_field(data, "model")),
        }
    if event == "turn.failed":
        return "turn.failed", {"message": _string_field(_field(data, "message"))}
    if event == "turn.cancelled":
        return "turn.cancelled", {
            "model": _optional_string(_field(data, "model")),
            "partialText": _optional_string(_field(data, "partial_text", "partialText")),
            "stopReason": _optional_string(_field(data, "stop_reason", "stopReason")),
        }
    if event == "status.updated":
        return "status.updated", {"message": _string_field(_field(data, "message")), "detail": _optional_string(_field(data, "detail"))}
    if event == "status.cleared":
        return "status.cleared", {}
    if event == "provider.input_tokens":
        return "provider.input_tokens", {
            "provider": _optional_string(_field(data, "provider")),
            "model": _optional_string(_field(data, "model")),
            "inputTokens": _optional_number(_field(data, "input_tokens", "inputTokens")),
            "source": _optional_string(_field(data, "source")),
            "baseInputTokens": _optional_number(_field(data, "base_input_tokens", "baseInputTokens")),
            "safetyMarginTokens": _optional_number(_field(data, "safety_margin_tokens", "safetyMarginTokens")),
        }
    if event == "provider.response_completed":
        return "provider.response_completed", {
            "provider": _optional_string(_field(data, "provider")),
            "providerProfile": _optional_string(_field(data, "provider_profile", "providerProfile")),
            "model": _optional_string(_field(data, "model")),
            "callIndex": _optional_number(_field(data, "call_index", "callIndex")),
            "usage": _field(data, "usage"),
            "stopReason": _optional_string(_field(data, "stop_reason", "stopReason")),
            "requestEstimateTokens": _optional_number(_field(data, "request_estimate_tokens", "requestEstimateTokens")),
            "requestEstimateSource": _optional_string(_field(data, "request_estimate_source", "requestEstimateSource")),
            "contextReliefMode": _optional_string(_field(data, "context_relief_mode", "contextReliefMode")),
            "contextReliefPressure": _optional_string(_field(data, "context_relief_pressure", "contextReliefPressure")),
            "contextReliefKind": _optional_string(_field(data, "context_relief_kind", "contextReliefKind")),
            "contextReliefProjectionHash": _optional_string(_field(data, "context_relief_projection_hash", "contextReliefProjectionHash")),
            "contextReliefEscalationRequested": _optional_boolean(_field(data, "context_relief_escalation_requested", "contextReliefEscalationRequested")),
        }
    if event == "provider.prompt_cache": return "provider.prompt_cache", promptCacheEventPayload(data)
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
            "toolUseId": _string_field(_field(data, "tool_use_id", "toolUseId")),
            "name": _string_field(_field(data, "name")),
            "output": _field(data, "output"),
            "error": _optional_boolean(_field(data, "error")),
            "originalItemCount": _optional_number(_field(data, "original_item_count", "originalItemCount")),
            "returnedItemCount": _optional_number(_field(data, "returned_item_count", "returnedItemCount")),
            "omittedItemCount": _optional_number(_field(data, "omitted_item_count", "omittedItemCount")),
            "itemsTruncated": _optional_boolean(_field(data, "items_truncated", "itemsTruncated")),
        }
    if event == "context.raw_pressure":
        return "context.raw_pressure", {
            "rawTokenEstimate": _optional_number(_field(data, "raw_token_estimate", "rawTokenEstimate")),
            "messageCount": _optional_number(_field(data, "message_count", "messageCount")),
            "assistantToolUseCount": _optional_number(_field(data, "assistant_tool_use_count", "assistantToolUseCount")),
            "toolResultCount": _optional_number(_field(data, "tool_result_count", "toolResultCount")),
            "messagesSinceLastCheckpoint": _optional_number(_field(data, "messages_since_last_checkpoint", "messagesSinceLastCheckpoint")),
            "toolResultsSinceLastCheckpoint": _optional_number(_field(data, "tool_results_since_last_checkpoint", "toolResultsSinceLastCheckpoint")),
            "rawTokenDeltaSinceLastCheckpoint": _optional_number(_field(data, "raw_token_delta_since_last_checkpoint", "rawTokenDeltaSinceLastCheckpoint")),
            "toolResultTokenEstimateSinceLastCheckpoint": _optional_number(
                _field(data, "tool_result_token_estimate_since_last_checkpoint", "toolResultTokenEstimateSinceLastCheckpoint")
            ),
            "largestToolResultTokenEstimate": _optional_number(_field(data, "largest_tool_result_token_estimate", "largestToolResultTokenEstimate")),
            "elapsedMsSinceLastCheckpoint": _optional_number(_field(data, "elapsed_ms_since_last_checkpoint", "elapsedMsSinceLastCheckpoint")),
            "hasDurableCheckpoint": _optional_boolean(_field(data, "has_durable_checkpoint", "hasDurableCheckpoint")),
            "latestCompactionAt": _optional_string(_field(data, "latest_compaction_at", "latestCompactionAt")),
        }
    if event == "compaction.decision":
        return "compaction.decision", {
            "shouldCheckpoint": _optional_boolean(_field(data, "should_checkpoint", "shouldCheckpoint")),
            "trigger": _optional_string(_field(data, "trigger")),
            "reason": _optional_string(_field(data, "reason")),
            "measurementScope": _optional_string(_field(data, "measurement_scope", "measurementScope")),
            "currentTokenEstimate": _optional_number(_field(data, "current_token_estimate", "currentTokenEstimate")),
            "currentTokenSource": _optional_string(_field(data, "current_token_source", "currentTokenSource")),
            "compactionThresholdTokens": _optional_number(_field(data, "compaction_threshold_tokens", "compactionThresholdTokens")),
            "maxContextLength": _optional_number(_field(data, "max_context_length", "maxContextLength")),
            "rawTokenEstimate": _optional_number(_field(data, "raw_token_estimate", "rawTokenEstimate")),
            "messageCount": _optional_number(_field(data, "message_count", "messageCount")),
            "messagesSinceLastCheckpoint": _optional_number(_field(data, "messages_since_last_checkpoint", "messagesSinceLastCheckpoint")),
            "toolResultsSinceLastCheckpoint": _optional_number(_field(data, "tool_results_since_last_checkpoint", "toolResultsSinceLastCheckpoint")),
            "rawTokenDeltaSinceLastCheckpoint": _optional_number(_field(data, "raw_token_delta_since_last_checkpoint", "rawTokenDeltaSinceLastCheckpoint")),
            "toolResultTokenEstimateSinceLastCheckpoint": _optional_number(_field(data, "tool_result_token_estimate_since_last_checkpoint", "toolResultTokenEstimateSinceLastCheckpoint")),
            "largestToolResultTokenEstimate": _optional_number(_field(data, "largest_tool_result_token_estimate", "largestToolResultTokenEstimate")),
            "hasDurableCheckpoint": _optional_boolean(_field(data, "has_durable_checkpoint", "hasDurableCheckpoint")),
            "latestCompactionAt": _optional_string(_field(data, "latest_compaction_at", "latestCompactionAt")),
        }
    if event == "permission.requested":
        return "permission.requested", {"summary": _optional_string(_field(data, "summary"))}
    if event == "permission.decided":
        return "permission.decided", {"decision": _optional_string(_field(data, "decision")), "summary": _optional_string(_field(data, "summary"))}
    if event == "sandbox.applied":
        return "sandbox.applied", {
            "backend": _optional_string(_field(data, "backend")),
            "enforcement": _optional_string(_field(data, "enforcement")),
            "writableRoots": _string_array(_field(data, "writable_roots", "writableRoots")),
            "allowNetwork": _optional_boolean(_field(data, "allow_network", "allowNetwork")),
        }
    if event == "sandbox.downgraded":
        return "sandbox.downgraded", {
            "requested": _optional_string(_field(data, "requested")),
            "actual": _optional_string(_field(data, "actual")),
            "backend": _optional_string(_field(data, "backend")),
            "reason": _optional_string(_field(data, "reason")),
            "writableRoots": _string_array(_field(data, "writable_roots", "writableRoots")),
            "allowNetwork": _optional_boolean(_field(data, "allow_network", "allowNetwork")),
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
    if event in {"plan.mcp_tool_allowed", "plan.mcp_tool_blocked"}:
        return event, {
            "server": _bounded_optional_string(_field(data, "server"), 256),
            "tool": _bounded_optional_string(_field(data, "tool"), 256),
            "reason": _bounded_optional_string(_field(data, "reason"), 64),
        }
    if event == "mcp.authorization_required":
        return event, {
            "server": _bounded_optional_string(_field(data, "server"), 256),
            "transport": _bounded_optional_string(_field(data, "transport"), 32),
            "authorizationUrl": _bounded_optional_string(_field(data, "authorization_url", "authorizationUrl"), 4096),
            "continuationId": _bounded_optional_string(_field(data, "continuation_id", "continuationId"), 128),
        }
    if event == "mcp.catalog_changed_applied":
        return event, {
            "servers": [_bounded_optional_string(item, 256) for item in _string_array(_field(data, "servers"))[:64]],
            "generation": _optional_number(_field(data, "generation")),
            "toolsChanged": _optional_boolean(_field(data, "tools_changed", "toolsChanged")),
            "resourcesChanged": _optional_boolean(_field(data, "resources_changed", "resourcesChanged")),
        }
    if event == "mcp.catalog_refresh_failed":
        return event, {
            "server": _bounded_optional_string(_field(data, "server"), 256),
            "catalog": _bounded_optional_string(_field(data, "catalog"), 32),
        }
    if event == "mcp.server_connected":
        return event, {
            "server": _bounded_optional_string(_field(data, "server"), 256),
            "transport": _bounded_optional_string(_field(data, "transport"), 32),
            "tools": _optional_number(_field(data, "tools")),
            "resources": _optional_number(_field(data, "resources")),
            "protocolVersion": _bounded_optional_string(_field(data, "protocol_version", "protocolVersion"), 32),
        }
    if event == "mcp.server_connection_failed":
        return event, {
            "server": _bounded_optional_string(_field(data, "server"), 256),
            "transport": _bounded_optional_string(_field(data, "transport"), 32),
            "reason": _bounded_optional_string(_field(data, "reason"), 512),
        }
    if event == "compaction.started":
        return "compaction.started", {
            "trigger": _optional_string(_field(data, "trigger")),
            "currentTokenEstimate": _optional_number(_field(data, "current_token_estimate", "currentTokenEstimate")),
            "currentTokenSource": _optional_string(_field(data, "current_token_source", "currentTokenSource")),
            "decisionReason": _optional_string(_field(data, "decision_reason", "decisionReason")),
            "rawTokenEstimate": _optional_number(_field(data, "raw_token_estimate", "rawTokenEstimate")),
            "messagesSinceLastCheckpoint": _optional_number(_field(data, "messages_since_last_checkpoint", "messagesSinceLastCheckpoint")),
            "toolResultsSinceLastCheckpoint": _optional_number(_field(data, "tool_results_since_last_checkpoint", "toolResultsSinceLastCheckpoint")),
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
            "durableCheckpointAvailable": _optional_boolean(_field(data, "durable_checkpoint_available", "durableCheckpointAvailable")),
            "compactions": _optional_number(_field(data, "compactions")),
            "reliefWithoutCheckpoint": _optional_boolean(_field(data, "relief_without_checkpoint", "reliefWithoutCheckpoint")),
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
            "durableCheckpointAvailable": _optional_boolean(_field(data, "durable_checkpoint_available", "durableCheckpointAvailable")),
            "compactions": _optional_number(_field(data, "compactions")),
            "reliefWithoutCheckpoint": _optional_boolean(_field(data, "relief_without_checkpoint", "reliefWithoutCheckpoint")),
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
    if event == "storage.lock_timeout":
        return "storage.lock_timeout", {
            "path": _string_field(_field(data, "path")),
            "layer": _optional_string(_field(data, "layer")),
            "timeoutMs": _optional_number(_field(data, "timeout_ms", "timeoutMs")),
            "action": _optional_string(_field(data, "action")),
        }
    if event == "budget.exceeded":
        return "budget.exceeded", {
            "estimatedCostUsd": _optional_number(_field(data, "estimated_cost_usd", "estimatedCostUsd")),
            "maxBudgetUsd": _optional_number(_field(data, "max_budget_usd", "maxBudgetUsd")),
            "model": _optional_string(_field(data, "model")),
        }
    return "runtime.unknown", {"eventName": event}
