from __future__ import annotations

from typing import Any

from .background_payloads import backgroundTypedPayload
from .progress_payloads import progressTypedPayload
from .provider_payloads import providerTypedPayload
from .shared import (
    KNOWN_RUNTIME_EVENTS,
    _bounded_optional_string,
    _camelize_record,
    _context_relief_mode_array,
    _context_relief_mode_field,
    _field,
    _optional_boolean,
    _optional_number,
    _optional_string,
    _permission_mode_field,
    _provider_field,
    _string_array,
    _string_field,
)
def _typed_payload(event: str, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    progress_payload = progressTypedPayload(event, data)
    if progress_payload is not None:
        return progress_payload
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
    provider_payload = providerTypedPayload(event, data)
    if provider_payload is not None:
        return provider_payload
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
    if event == "tool.result_budget":
        return event, {
            "effectiveResultBudgetBytes": _optional_number(_field(data, "effective_result_budget_bytes", "effectiveResultBudgetBytes")),
            "batchBudgetBytes": _optional_number(_field(data, "batch_budget_bytes", "batchBudgetBytes")),
            "batchUsedBytes": _optional_number(_field(data, "batch_used_bytes", "batchUsedBytes")),
            "originalBytes": _optional_number(
                _field(data, "original_bytes", "originalBytes")
            ),
            "finalBytes": _optional_number(_field(data, "final_bytes", "finalBytes")),
            "truncatedResultCount": _optional_number(
                _field(data, "truncated_result_count", "truncatedResultCount")
            ),
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
            "compactionTargetTokens": _optional_number(_field(data, "compaction_target_tokens", "compactionTargetTokens")),
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
    if event == "compaction.headroom":
        return "compaction.headroom", {
            "status": _bounded_optional_string(_field(data, "status"), 64),
            "reason": _bounded_optional_string(_field(data, "reason"), 64),
            "measurementSource": _bounded_optional_string(
                _field(data, "measurement_source", "measurementSource"), 64
            ),
            "inputTokens": _optional_number(_field(data, "input_tokens", "inputTokens")),
            "irreducibleInputTokens": _optional_number(
                _field(data, "irreducible_input_tokens", "irreducibleInputTokens")
            ),
            "messageInputTokens": _optional_number(
                _field(data, "message_input_tokens", "messageInputTokens")
            ),
            "imageInputTokens": _optional_number(
                _field(data, "image_input_tokens", "imageInputTokens")
            ),
            "compactionThresholdTokens": _optional_number(
                _field(data, "compaction_threshold_tokens", "compactionThresholdTokens")
            ),
            "compactionTargetTokens": _optional_number(
                _field(data, "compaction_target_tokens", "compactionTargetTokens")
            ),
            "remainingHeadroomTokens": _optional_number(
                _field(data, "remaining_headroom_tokens", "remainingHeadroomTokens")
            ),
            "effectiveMaxOutputTokens": _optional_number(
                _field(data, "effective_max_output_tokens", "effectiveMaxOutputTokens")
            ),
            "minUsefulOutputTokens": _optional_number(
                _field(data, "min_useful_output_tokens", "minUsefulOutputTokens")
            ),
            "compactionOutcome": _bounded_optional_string(
                _field(data, "compaction_outcome", "compactionOutcome"), 32
            ),
            "compactionOutcomeReason": _bounded_optional_string(
                _field(data, "compaction_outcome_reason", "compactionOutcomeReason"), 64
            ),
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
    background_payload = backgroundTypedPayload(event, data)
    if background_payload is not None:
        return background_payload
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
