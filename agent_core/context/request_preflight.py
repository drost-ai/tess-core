from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .._image_redaction import redactImagePayloads
from ..message_origin import (
    isPlainUserTurnAnchor as _is_plain_user_turn_anchor,
    messageForStableFingerprint as _message_for_stable_fingerprint,
)
from .request_preflight_relief import (
    interactiveReliefProjection as _interactive_relief_projection,
)
SCHEMA_VERSION = 1
RECEIPT_MAX_BYTES = 16 * 1024
_CATEGORY_NAMES = (
    "systemIdentity",
    "selectedSkills",
    "toolDefinitions",
    "checkpoint",
    "continuationOverlays",
    "recentHistory",
    "currentText",
    "currentMedia",
    "pendingToolResults",
    "protocolOverhead",
    "unattributed",
)

def buildRequestPreflight(
    *,
    request: dict[str, Any],
    measurement: dict[str, Any],
    outputBudget: dict[str, Any],
    contextBudget: dict[str, Any],
    continuation: dict[str, Any],
    session: dict[str, Any],
    baseSystemPrompt: str,
    preflightHints: dict[str, Any] | None = None,
    dynamicContextInMessages: bool = False,
    compactionOutcome: dict[str, Any] | None = None,
    providerProfileName: str | None = None,
) -> dict[str, Any]:
    """Project the already-admitted provider request into bounded numeric provenance."""

    hints = preflightHints if isinstance(preflightHints, dict) else {}
    segments = _continuation_segments(continuation)
    fixed_pool = _count(measurement.get("estimatedFixedInputTokens"))
    message_pool = _count(measurement.get("estimatedMessageInputTokens"))
    image_pool = _count(measurement.get("estimatedImageInputTokens"))
    base_system_tokens = _estimate_text_tokens(baseSystemPrompt)
    skill_tokens, skill_provenance = _selected_skill_tokens(
        baseSystemPrompt,
        hints,
        base_system_tokens,
    )
    projected_system_tokens = _estimate_text_tokens(str(request.get("system") or ""))
    dynamic_system_tokens = (
        0
        if dynamicContextInMessages
        else segments["checkpointTokens"] + segments["continuationOverlayTokens"]
    )
    identity_tokens = max(0, projected_system_tokens - dynamic_system_tokens)

    fixed_ideals = {
        "systemIdentity": max(0, identity_tokens - min(skill_tokens, identity_tokens)),
        "selectedSkills": min(skill_tokens, identity_tokens),
        "toolDefinitions": _estimate_json_tokens(request.get("tools") or []),
        "checkpoint": 0 if dynamicContextInMessages else segments["checkpointTokens"],
        "continuationOverlays": (
            0 if dynamicContextInMessages else segments["continuationOverlayTokens"]
        ),
    }
    fixed_categories, fixed_protocol = _fit_content_estimates(fixed_ideals, fixed_pool)

    message_ideals = _classify_messages(
        request.get("messages") or [],
        continuation=continuation,
        dynamic_context_in_messages=dynamicContextInMessages,
    )
    if dynamicContextInMessages:
        message_ideals["checkpoint"] = segments["checkpointTokens"]
        message_ideals["continuationOverlays"] = segments["continuationOverlayTokens"]
    message_categories, message_protocol = _fit_content_estimates(
        message_ideals,
        message_pool,
    )

    categories = {name: 0 for name in _CATEGORY_NAMES}
    for source in (fixed_categories, message_categories):
        for name, value in source.items():
            categories[name] += value
    categories["currentMedia"] = image_pool
    categories["protocolOverhead"] = fixed_protocol + message_protocol

    component_total = fixed_pool + message_pool + image_pool
    measured_input = _count(measurement.get("inputTokens"))
    measurement_delta = measured_input - component_total
    categories["unattributed"] = max(0, measurement_delta)

    compaction_state = (
        (session.get("metadata") or {}).get("compactionState")
        if isinstance(session.get("metadata"), dict)
        else None
    )
    compaction_state = compaction_state if isinstance(compaction_state, dict) else {}
    relief = continuation.get("relief")
    relief = relief if isinstance(relief, dict) else {}
    receipt = {
        "schemaVersion": SCHEMA_VERSION,
        "provider": measurement.get("provider") or session.get("provider"),
        "profile": providerProfileName,
        "model": measurement.get("model") or request.get("model"),
        "measurement": {
            "source": measurement.get("source"),
            "measuredAt": measurement.get("measuredAt"),
            "inputTokens": measured_input,
            "baseInputTokens": _optional_count(measurement.get("baseInputTokens")),
            "safetyMarginTokens": _optional_count(
                measurement.get("safetyMarginTokens")
            ),
            "exactCountFallback": bool(measurement.get("exactCountFallback")),
            "componentEstimateTokens": component_total,
            "measurementDeltaTokens": measurement_delta,
        },
        "admission": {
            "status": contextBudget.get("status"),
            "reason": contextBudget.get("reason"),
            "compactionThresholdTokens": _count(
                contextBudget.get("compactionThresholdTokens")
            ),
            "compactionTargetTokens": _count(
                contextBudget.get("compactionTargetTokens")
            ),
            "remainingHeadroomTokens": _signed_count(
                contextBudget.get("remainingHeadroomTokens")
            ),
            "configuredMaxOutputTokens": _count(
                outputBudget.get("configuredMaxOutputTokens")
            ),
            "effectiveMaxOutputTokens": _count(
                outputBudget.get("effectiveMaxOutputTokens")
            ),
            "minimumUsefulOutputTokens": _count(
                outputBudget.get("minUsefulOutputTokens")
            ),
            "remainingOutputTokens": _signed_count(
                outputBudget.get("remainingOutputTokens")
            ),
            "outputBudgetClamped": bool(outputBudget.get("clamped")),
        },
        "compaction": {
            "generation": _count(compaction_state.get("latestCompactionGeneration")),
            "cutoffMessageIndex": _count(
                compaction_state.get("compactedThroughMessageIndex")
            ),
            "outcome": (compactionOutcome or {}).get("status"),
            "outcomeReason": (compactionOutcome or {}).get("reason"),
            "reliefMode": relief.get("selectedMode") or "none",
            "reliefApplied": bool(relief.get("applied")),
        },
        "interactiveRelief": _interactive_relief_projection(
            continuation,
            measured_input,
        ),
        "categories": categories,
        "provenance": {
            "selectedSkills": skill_provenance,
            "continuationSegments": segments["provenance"],
            "currentTurn": _current_turn_provenance(hints, session),
        },
        "requestFingerprint": _request_fingerprint(request),
    }
    _validate_receipt(receipt)
    return receipt


def preflightEventPayload(session_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "schema_version": receipt.get("schemaVersion"),
        "provider": receipt.get("provider"),
        "profile": receipt.get("profile"),
        "model": receipt.get("model"),
        "measurement": copy.deepcopy(receipt.get("measurement")),
        "admission": copy.deepcopy(receipt.get("admission")),
        "compaction": copy.deepcopy(receipt.get("compaction")),
        "interactive_relief": copy.deepcopy(receipt.get("interactiveRelief")),
        "categories": copy.deepcopy(receipt.get("categories")),
        "provenance": copy.deepcopy(receipt.get("provenance")),
        "request_fingerprint": receipt.get("requestFingerprint"),
    }


def imageMeasurementEventFields(measurement: dict[str, Any]) -> dict[str, Any]:
    if not measurement.get("imageCount"):
        return {}
    return {
        "image_count": measurement["imageCount"],
        "image_decoded_bytes": measurement.get("imageDecodedBytes"),
        "image_encoded_request_bytes": measurement.get("imageEncodedRequestBytes"),
        "image_input_tokens": measurement.get("imageInputTokens"),
        "image_set_fingerprint": measurement.get("imageSetFingerprint"),
        "image_measurements": measurement.get("imageMeasurements"),
    }


def _continuation_segments(continuation: dict[str, Any]) -> dict[str, Any]:
    raw = continuation.get("preflightSegments")
    raw = raw if isinstance(raw, dict) else {}
    checkpoint = _count(raw.get("checkpointTokens"))
    overlay_parts = {
        "continuationOverlayTokens": _count(raw.get("continuationOverlayTokens")),
        "retrievedMemoryTokens": _count(raw.get("retrievedMemoryTokens")),
        "reliefTokens": _count(raw.get("reliefTokens")),
    }
    return {
        "checkpointTokens": checkpoint,
        "continuationOverlayTokens": sum(overlay_parts.values()),
        "provenance": {
            "status": "measured" if raw else "unknown",
            "checkpointTokens": checkpoint,
            **overlay_parts,
        },
    }


def _selected_skill_tokens(
    base_system_prompt: str,
    hints: dict[str, Any],
    base_system_tokens: int,
) -> tuple[int, dict[str, Any]]:
    expected_hash = hashlib.sha256(base_system_prompt.encode("utf-8")).hexdigest()
    prompt_hash = hints.get("systemPromptHash")
    raw_chars = hints.get("selectedSkillChars")
    valid_chars = (
        isinstance(raw_chars, int)
        and not isinstance(raw_chars, bool)
        and 0 <= raw_chars <= len(base_system_prompt)
    )
    trusted = (
        isinstance(prompt_hash, str) and prompt_hash == expected_hash and valid_chars
    )
    if not trusted:
        return 0, {
            "status": "unknown",
            "trusted": False,
            "reason": _hint_rejection_reason(prompt_hash, expected_hash, valid_chars),
        }
    return min(base_system_tokens, _estimate_text_tokens("x" * raw_chars)), {
        "status": "caller_bound",
        "trusted": True,
        "systemPromptHash": expected_hash,
        "selectedSkillChars": raw_chars,
    }


def _hint_rejection_reason(
    prompt_hash: Any, expected_hash: str, valid_chars: bool
) -> str:
    if prompt_hash is None:
        return "not_provided"
    if not isinstance(prompt_hash, str) or prompt_hash != expected_hash:
        return "prompt_hash_mismatch"
    if not valid_chars:
        return "invalid_selected_skill_chars"
    return "unbound"


def _classify_messages(
    messages: list[Any],
    *,
    continuation: dict[str, Any],
    dynamic_context_in_messages: bool,
) -> dict[str, int]:
    current_index = _find_current_plain_user_index(messages)
    values = {
        "recentHistory": 0,
        "currentText": 0,
        "pendingToolResults": 0,
        "checkpoint": 0,
        "continuationOverlays": 0,
    }
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            values["recentHistory"] += _estimate_json_tokens(message)
            continue
        content = message.get("content")
        if index == current_index:
            values["currentText"] += _content_tokens(
                content,
                include_tool_results=False,
            )
            continue
        if index > current_index >= 0:
            pending = _tool_result_tokens(content)
            values["pendingToolResults"] += pending
            values["recentHistory"] += max(0, _content_tokens(content) - pending)
            continue
        values["recentHistory"] += _content_tokens(content)
    if dynamic_context_in_messages and current_index >= 0:
        dynamic_tokens = _dynamic_context_message_tokens(
            continuation.get("systemContext")
        )
        values["currentText"] = max(0, values["currentText"] - dynamic_tokens)
    return values


def _find_current_plain_user_index(messages: list[Any]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if _is_plain_user_turn_anchor(message):
            return index
    return -1


def _content_tokens(content: Any, *, include_tool_results: bool = True) -> int:
    if isinstance(content, str):
        return _estimate_text_tokens(content)
    if not isinstance(content, list):
        return _estimate_json_tokens(content)
    total = 0
    for block in content:
        if not isinstance(block, dict):
            total += _estimate_json_tokens(block)
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"image", "input_image"}:
            continue
        if block_type == "tool_result" and not include_tool_results:
            continue
        if block_type in {"text", "input_text", "output_text"}:
            total += _estimate_text_tokens(str(block.get("text") or ""))
        elif block_type == "tool_result":
            total += _content_tokens(block.get("content"))
        else:
            total += _estimate_json_tokens(redactImagePayloads(block))
    return total


def _tool_result_tokens(content: Any) -> int:
    if not isinstance(content, list):
        return 0
    return sum(
        _content_tokens(block.get("content"))
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )


def _dynamic_context_message_tokens(system_context: Any) -> int:
    text = str(system_context or "").strip()
    if not text:
        return 0
    return _estimate_text_tokens(
        f"[Dynamic continuation context]\n{text}\n[/Dynamic continuation context]"
    )


def _fit_content_estimates(
    ideals: dict[str, int],
    pool: int,
) -> tuple[dict[str, int], int]:
    clean = {key: _count(value) for key, value in ideals.items()}
    total = sum(clean.values())
    if total <= pool:
        return clean, pool - total
    if total <= 0 or pool <= 0:
        return {key: 0 for key in clean}, max(0, pool)
    scaled = {key: (value * pool) // total for key, value in clean.items()}
    remainder = pool - sum(scaled.values())
    for key in sorted(clean, key=lambda item: clean[item], reverse=True):
        if remainder <= 0:
            break
        if clean[key] > scaled[key]:
            scaled[key] += 1
            remainder -= 1
    return scaled, 0


def _current_turn_provenance(
    hints: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    turn_id = hints.get("currentTurnId")
    message_index = hints.get("currentMessageIndex")
    valid_id = isinstance(turn_id, str) and 0 < len(turn_id) <= 256
    valid_index = (
        isinstance(message_index, int)
        and not isinstance(message_index, bool)
        and 0 <= message_index < len(session.get("messages") or [])
    )
    bound_message = (
        (session.get("messages") or [])[message_index] if valid_index else None
    )
    metadata = (
        bound_message.get("metadata")
        if isinstance(bound_message, dict)
        and isinstance(bound_message.get("metadata"), dict)
        else {}
    )
    host = (
        metadata.get("host") if isinstance(metadata.get("host"), dict) else {}
    )
    bound_turn_id = (
        host.get("turn_id")
        or metadata.get("turnId")
        or metadata.get("turn_id")
        or metadata.get("logicalTurnId")
    )
    latest_plain_user = _find_current_plain_user_index(session.get("messages") or [])
    trusted = bool(
        valid_id
        and valid_index
        and message_index == latest_plain_user
        and bound_turn_id == turn_id
    )
    return {
        "status": "caller_bound" if trusted else "inferred",
        "trusted": trusted,
        "turnIdHash": (
            hashlib.sha256(turn_id.encode("utf-8")).hexdigest() if trusted else None
        ),
        "messageIndex": message_index if trusted else None,
    }


def _request_fingerprint(request: dict[str, Any]) -> str:
    projection = {
        "model": request.get("model"),
        "effort": request.get("effort"),
        "system": request.get("system"),
        "messages": [
            _message_for_stable_fingerprint(message)
            for message in request.get("messages") or []
        ],
        "tools": request.get("tools") or [],
    }
    serialized = json.dumps(
        redactImagePayloads(projection),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _estimate_json_tokens(value: Any) -> int:
    return _estimate_text_tokens(
        json.dumps(
            redactImagePayloads(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _estimate_text_tokens(value: str) -> int:
    return (len(value) + 3) // 4


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _signed_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _optional_count(value: Any) -> int | None:
    return None if value is None else _count(value)


def _validate_receipt(receipt: dict[str, Any]) -> None:
    categories = receipt.get("categories") or {}
    if set(categories) != set(_CATEGORY_NAMES):
        raise ValueError("request preflight categories are incomplete")
    if any(not isinstance(value, int) or value < 0 for value in categories.values()):
        raise ValueError("request preflight categories must be nonnegative integers")
    serialized = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > RECEIPT_MAX_BYTES:
        raise ValueError("request preflight receipt exceeds 16 KiB")
