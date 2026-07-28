from __future__ import annotations

from typing import Any

from .shared import (
    _field,
    _optional_boolean,
    _optional_number,
    _optional_string,
)


def providerEventPayload(
    event: str, data: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    if event == "message.assistant.partial":
        return event, _without_none({
            "text": _optional_string(_field(data, "text")),
            "textDelta": _optional_string(_field(data, "text_delta", "textDelta")),
            "logicalCallId": _optional_string(
                _field(data, "logical_call_id", "logicalCallId")
            ),
            "generation": _optional_number(_field(data, "generation")),
            "responseId": _optional_string(_field(data, "response_id", "responseId")),
            "sequenceNumber": _optional_number(
                _field(data, "sequence_number", "sequenceNumber")
            ),
            "deliveryMode": _optional_string(
                _field(data, "delivery_mode", "deliveryMode")
            ),
        })
    if event in {"provider.retrying", "provider.recovered"}:
        return event, _without_none({
            "providerLabel": _optional_string(
                _field(data, "provider_label", "providerLabel")
            ),
            "nextAttempt": _optional_number(_field(data, "next_attempt", "nextAttempt")),
            "delayMs": _optional_number(_field(data, "delay_ms", "delayMs")),
            "attempts": _optional_number(_field(data, "attempts")),
            "category": _optional_string(_field(data, "category")),
            "logicalCallId": _optional_string(
                _field(data, "logical_call_id", "logicalCallId")
            ),
            "generation": _optional_number(_field(data, "generation")),
            "requestId": _optional_string(_field(data, "request_id", "requestId")),
            "deliveryMode": _optional_string(
                _field(data, "partial_output_mode", "partialOutputMode")
            ),
        })
    if event in {"provider.attempt_superseding", "provider.attempt_superseded"}:
        return event, _without_none({
            "provider": _optional_string(_field(data, "provider")),
            "category": _optional_string(_field(data, "category")),
            "phase": _optional_string(_field(data, "phase")),
            "logicalCallId": _optional_string(
                _field(data, "logical_call_id", "logicalCallId")
            ),
            "generation": _optional_number(_field(data, "generation")),
            "nextGeneration": _optional_number(
                _field(data, "next_generation", "nextGeneration")
            ),
            "requestId": _optional_string(_field(data, "request_id", "requestId")),
            "deliveryMode": _optional_string(
                _field(data, "partial_output_mode", "partialOutputMode")
            ),
        })
    if event == "provider.failed":
        return event, _without_none({
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
            "errorType": _optional_string(_field(data, "error_type", "errorType")),
            "requestId": _optional_string(_field(data, "request_id", "requestId")),
            "logicalCallId": _optional_string(
                _field(data, "logical_call_id", "logicalCallId")
            ),
            "generation": _optional_number(_field(data, "generation")),
            "deliveryMode": _optional_string(
                _field(data, "partial_output_mode", "partialOutputMode")
            ),
            "recoveryFailure": _optional_string(
                _field(data, "recovery_failure", "recoveryFailure")
            ),
            "interruptedPreview": _field(
                data, "interrupted_preview", "interruptedPreview"
            ),
        })
    return None


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}
