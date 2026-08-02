from __future__ import annotations

from .prompt_cache import promptCacheEventPayload
from .shared import *


def providerTypedPayload(
    event: str,
    data: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    if event in {
        "message.assistant.partial",
        "message.assistant.thinking.partial",
    }:
        return event, _without_none(
            {
                "text": _optional_string(_field(data, "text")),
                "textDelta": _optional_string(
                    _field(data, "text_delta", "textDelta")
                ),
                "thinking": _optional_string(_field(data, "thinking")),
                "thinkingDelta": _optional_string(
                    _field(data, "thinking_delta", "thinkingDelta")
                ),
                "reasoningKind": _closed_reasoning_kind(
                    _field(data, "reasoning_kind", "reasoningKind")
                ),
                "userVisible": _optional_boolean(
                    _field(data, "user_visible", "userVisible")
                ),
                "providerEventType": _closed_reasoning_provider_event_type(
                    _field(data, "provider_event_type", "providerEventType")
                ),
                **_partial_output_lineage(data),
            }
        )
    if event == "provider.retrying":
        return event, {
            "providerLabel": _optional_string(
                _field(data, "provider_label", "providerLabel")
            ),
            "nextAttempt": _optional_number(
                _field(data, "next_attempt", "nextAttempt")
            ),
            "delayMs": _optional_number(_field(data, "delay_ms", "delayMs")),
            **_provider_recovery_lineage(data),
        }
    if event == "provider.recovered":
        return event, {
            "providerLabel": _optional_string(
                _field(data, "provider_label", "providerLabel")
            ),
            "attempts": _optional_number(_field(data, "attempts")),
            **_provider_recovery_lineage(data),
        }
    if event == "provider.consumer_sink_degraded":
        return event, _without_none(
            {
                "component": _optional_string(_field(data, "component")),
                "operation": _optional_string(_field(data, "operation")),
                "reason": _optional_string(_field(data, "reason")),
                "responseId": _optional_string(
                    _field(data, "response_id", "responseId")
                ),
                **_provider_recovery_lineage(data),
            }
        )
    if event in {"provider.attempt_superseding", "provider.attempt_superseded"}:
        return event, _without_none(
            {
                "provider": _optional_string(_field(data, "provider")),
                "category": _optional_string(_field(data, "category")),
                "phase": _optional_string(_field(data, "phase")),
                "nextGeneration": _optional_number(
                    _field(data, "next_generation", "nextGeneration")
                ),
                **_provider_recovery_lineage(data),
            }
        )
    if event == "provider.failed":
        return event, {
            "provider": _optional_string(_field(data, "provider")),
            "providerLabel": _optional_string(
                _field(data, "provider_label", "providerLabel")
            ),
            "category": _optional_string(_field(data, "category")),
            "phase": _optional_string(_field(data, "phase")),
            "message": _optional_string(_field(data, "message")),
            "retryable": _optional_boolean(_field(data, "retryable")),
            "attempt": _optional_number(_field(data, "attempt")),
            "status": _optional_number(_field(data, "status")),
            "code": _optional_string(_field(data, "code")),
            "errorType": _optional_string(
                _field(data, "error_type", "errorType")
            ),
            **_provider_recovery_lineage(data),
            **_without_none(
                {
                    "recoveryFailure": _optional_string(
                        _field(data, "recovery_failure", "recoveryFailure")
                    ),
                    "interruptedPreview": _field(
                        data,
                        "interrupted_preview",
                        "interruptedPreview",
                    ),
                }
            ),
        }
    if event == "provider.activity":
        return event, {
            "logicalTurnId": _bounded_optional_string(
                _field(data, "logical_turn_id", "logicalTurnId"),
                128,
            ),
            "logicalCallId": _bounded_optional_string(
                _field(data, "logical_call_id", "logicalCallId"),
                128,
            ),
            "attempt": _optional_number(_field(data, "attempt")),
            "provider": _bounded_optional_string(
                _field(data, "provider"),
                64,
            ),
            "profile": _bounded_optional_string(
                _field(data, "profile"),
                128,
            ),
            "model": _bounded_optional_string(_field(data, "model"), 128),
            "phase": _bounded_optional_string(_field(data, "phase"), 32),
            "activityKind": _bounded_optional_string(
                _field(data, "activity_kind", "activityKind"),
                32,
            ),
            "monotonicSequence": _optional_number(
                _field(data, "monotonic_sequence", "monotonicSequence")
            ),
            "occurredAt": _bounded_optional_string(
                _field(data, "occurred_at", "occurredAt"),
                64,
            ),
        }
    if event == "provider.input_tokens":
        return event, {
            "provider": _optional_string(_field(data, "provider")),
            "model": _optional_string(_field(data, "model")),
            "inputTokens": _optional_number(
                _field(data, "input_tokens", "inputTokens")
            ),
            "source": _optional_string(_field(data, "source")),
            "baseInputTokens": _optional_number(
                _field(data, "base_input_tokens", "baseInputTokens")
            ),
            "safetyMarginTokens": _optional_number(
                _field(data, "safety_margin_tokens", "safetyMarginTokens")
            ),
        }
    if event == "provider.request_preflight":
        return event, {
            "schemaVersion": _optional_number(
                _field(data, "schema_version", "schemaVersion")
            ),
            "provider": _optional_string(_field(data, "provider")),
            "profile": _optional_string(_field(data, "profile")),
            "model": _optional_string(_field(data, "model")),
            "requestFingerprint": _optional_string(
                _field(data, "request_fingerprint", "requestFingerprint")
            ),
            "measurement": _field(data, "measurement"),
            "admission": _field(data, "admission"),
            "categories": _field(data, "categories"),
            "compaction": _field(data, "compaction"),
            "interactiveRelief": _field(
                data, "interactive_relief", "interactiveRelief"
            ),
            "provenance": _field(data, "provenance"),
        }
    if event == "provider.response_completed":
        return event, {
            "provider": _optional_string(_field(data, "provider")),
            "providerProfile": _optional_string(
                _field(data, "provider_profile", "providerProfile")
            ),
            "model": _optional_string(_field(data, "model")),
            "callIndex": _optional_number(
                _field(data, "call_index", "callIndex")
            ),
            "usage": _field(data, "usage"),
            "stopReason": _optional_string(
                _field(data, "stop_reason", "stopReason")
            ),
            "requestEstimateTokens": _optional_number(
                _field(
                    data,
                    "request_estimate_tokens",
                    "requestEstimateTokens",
                )
            ),
            "requestEstimateSource": _optional_string(
                _field(
                    data,
                    "request_estimate_source",
                    "requestEstimateSource",
                )
            ),
            "contextReliefMode": _optional_string(
                _field(data, "context_relief_mode", "contextReliefMode")
            ),
            "contextReliefPressure": _optional_string(
                _field(
                    data,
                    "context_relief_pressure",
                    "contextReliefPressure",
                )
            ),
            "contextReliefKind": _optional_string(
                _field(data, "context_relief_kind", "contextReliefKind")
            ),
            "contextReliefProjectionHash": _optional_string(
                _field(
                    data,
                    "context_relief_projection_hash",
                    "contextReliefProjectionHash",
                )
            ),
            "contextReliefEscalationRequested": _optional_boolean(
                _field(
                    data,
                    "context_relief_escalation_requested",
                    "contextReliefEscalationRequested",
                )
            ),
        }
    if event == "provider.prompt_cache":
        return event, promptCacheEventPayload(data)
    if event == "provider.fallback":
        return event, {
            "provider": _optional_string(_field(data, "provider")),
            "model": _optional_string(_field(data, "model")),
            "error": _optional_string(_field(data, "error")),
        }
    return None


def _partial_output_lineage(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "logicalCallId": _optional_string(
            _field(data, "logical_call_id", "logicalCallId")
        ),
        "generation": _optional_number(_field(data, "generation")),
        "responseId": _optional_string(
            _field(data, "response_id", "responseId")
        ),
        "sequenceNumber": _optional_number(
            _field(data, "sequence_number", "sequenceNumber")
        ),
        "deliveryMode": _optional_string(
            _field(data, "delivery_mode", "deliveryMode")
        ),
    }


def _provider_recovery_lineage(data: dict[str, Any]) -> dict[str, Any]:
    return _without_none(
        {
            "category": _optional_string(_field(data, "category")),
            "logicalCallId": _optional_string(
                _field(data, "logical_call_id", "logicalCallId")
            ),
            "generation": _optional_number(_field(data, "generation")),
            "requestId": _optional_string(
                _field(data, "request_id", "requestId")
            ),
            "deliveryMode": _optional_string(
                _field(data, "partial_output_mode", "partialOutputMode")
            ),
        }
    )


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _closed_reasoning_kind(value: Any) -> str | None:
    return value if value in {"summary", "raw_thinking", "unknown"} else None


def _closed_reasoning_provider_event_type(value: Any) -> str | None:
    allowed = {
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
        "content_block_delta.thinking_delta",
        "provider.interrupted_reasoning",
    }
    return value if value in allowed else None


__all__ = ["providerTypedPayload"]
