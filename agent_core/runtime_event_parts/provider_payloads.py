from __future__ import annotations

from .prompt_cache import promptCacheEventPayload
from .shared import *


def providerTypedPayload(
    event: str,
    data: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    if event == "provider.retrying":
        return event, {
            "providerLabel": _optional_string(
                _field(data, "provider_label", "providerLabel")
            ),
            "nextAttempt": _optional_number(
                _field(data, "next_attempt", "nextAttempt")
            ),
            "delayMs": _optional_number(_field(data, "delay_ms", "delayMs")),
        }
    if event == "provider.recovered":
        return event, {
            "providerLabel": _optional_string(
                _field(data, "provider_label", "providerLabel")
            ),
            "attempts": _optional_number(_field(data, "attempts")),
        }
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


__all__ = ["providerTypedPayload"]
