from __future__ import annotations

from .shared import (
    Any,
    _bounded_optional_string,
    _field,
    _optional_boolean,
    _optional_number,
)


def progressTypedPayload(
    event: str,
    data: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    if event != "agent.no_progress":
        return None
    return event, {
        "logicalTurnId": _bounded_optional_string(
            _field(data, "logical_turn_id", "logicalTurnId"),
            256,
        ),
        "reason": _bounded_optional_string(_field(data, "reason"), 64),
        "artifactId": _bounded_optional_string(
            _field(data, "artifact_id", "artifactId"),
            128,
        ),
        "cycleFingerprint": _bounded_optional_string(
            _field(data, "cycle_fingerprint", "cycleFingerprint"),
            64,
        ),
        "providerCallIndex": _optional_number(
            _field(data, "provider_call_index", "providerCallIndex")
        ),
        "toolBatchIndex": _optional_number(
            _field(data, "tool_batch_index", "toolBatchIndex")
        ),
        "consecutiveNoProgress": _optional_number(
            _field(data, "consecutive_no_progress", "consecutiveNoProgress")
        ),
        "recoveredTerminal": _optional_boolean(
            _field(data, "recovered_terminal", "recoveredTerminal")
        ),
    }
