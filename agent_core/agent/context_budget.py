from __future__ import annotations

from typing import Any

from ..providers.errors import create_provider_failure_record, create_provider_request_error


def assess_context_headroom(
    *,
    measurement: dict[str, Any],
    policy: dict[str, Any],
    outputBudget: dict[str, Any],
    compactionOutcome: dict[str, Any] | None,
) -> dict[str, Any]:
    input_tokens = _count(measurement.get("inputTokens"))
    threshold = _count(policy.get("compactionThresholdTokens"))
    target = _count(policy.get("compactionTargetTokens"))
    fixed = _count(measurement.get("estimatedFixedInputTokens"))
    remaining = threshold - input_tokens
    status = "ready"
    reason = None
    if input_tokens >= threshold or outputBudget.get("insufficient"):
        status = "context_capacity_exhausted"
        reason = _impossible_headroom_reason(
            measurement=measurement,
            target=target,
            fixed=fixed,
            outcome=compactionOutcome,
        )
    elif input_tokens > target:
        status = "headroom_degraded"
        reason = _impossible_headroom_reason(
            measurement=measurement,
            target=target,
            fixed=fixed,
            outcome=compactionOutcome,
        )
    return {
        "status": status,
        "reason": reason,
        "measurementSource": measurement.get("source"),
        "inputTokens": input_tokens,
        "irreducibleInputTokens": fixed,
        "messageInputTokens": _count(measurement.get("estimatedMessageInputTokens")),
        "imageInputTokens": _count(measurement.get("estimatedImageInputTokens")),
        "compactionThresholdTokens": threshold,
        "compactionTargetTokens": target,
        "remainingHeadroomTokens": remaining,
        "effectiveMaxOutputTokens": _count(outputBudget.get("effectiveMaxOutputTokens")),
        "minUsefulOutputTokens": _count(outputBudget.get("minUsefulOutputTokens")),
        "compactionOutcome": (compactionOutcome or {}).get("status"),
        "compactionOutcomeReason": (compactionOutcome or {}).get("reason"),
    }


def enforce_context_headroom(
    *,
    config: dict[str, Any],
    assessment: dict[str, Any],
) -> None:
    if assessment.get("status") != "context_capacity_exhausted":
        return
    reason = str(assessment.get("reason") or "context_capacity_exhausted")
    raise create_provider_request_error(
        create_provider_failure_record(
            provider=config.get("provider"),
            providerLabel=config.get("providerProfileName") or config.get("provider"),
            phase="request_build",
            category="client_request",
            retryable=False,
            attempt=1,
            hadAssistantOutput=False,
            hadToolUse=False,
            code="context_capacity_exhausted",
            rawMessage=(
                "Provider dispatch blocked because the complete request cannot preserve "
                f"the configured context budget ({reason}; "
                f"input={assessment.get('inputTokens')}, "
                f"target={assessment.get('compactionTargetTokens')}, "
                f"threshold={assessment.get('compactionThresholdTokens')})."
            ),
        )
    )


def apply_provider_token_budget(
    request: dict[str, Any],
    budget: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if budget.get("insufficient"):
        raise create_provider_request_error(
            create_provider_failure_record(
                {
                    "provider": config.get("provider"),
                    "providerLabel": config.get("providerProfileName"),
                    "phase": "request_build",
                    "category": "client_request",
                    "retryable": False,
                    "attempt": 1,
                    "hadAssistantOutput": False,
                    "hadToolUse": False,
                    "rawMessage": (
                        f"Provider profile '{config.get('providerProfileName')}' "
                        f"has maxContextLength={budget.get('maxContextLength')}, "
                        f"estimated input tokens={budget.get('estimatedInputTokens')}, "
                        f"safety margin={budget.get('safetyMarginTokens')}, and only "
                        f"{budget.get('remainingOutputTokens')} output tokens remaining. "
                        f"At least {budget.get('minUsefulOutputTokens')} output tokens "
                        "are required before dispatch."
                    ),
                }
            )
        )
    return {**request, "maxOutputTokens": budget.get("effectiveMaxOutputTokens")}


def headroom_event_payload(
    session_id: str,
    assessment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "status": assessment.get("status"),
        "reason": assessment.get("reason"),
        "measurement_source": assessment.get("measurementSource"),
        "input_tokens": assessment.get("inputTokens"),
        "irreducible_input_tokens": assessment.get("irreducibleInputTokens"),
        "message_input_tokens": assessment.get("messageInputTokens"),
        "image_input_tokens": assessment.get("imageInputTokens"),
        "compaction_threshold_tokens": assessment.get("compactionThresholdTokens"),
        "compaction_target_tokens": assessment.get("compactionTargetTokens"),
        "remaining_headroom_tokens": assessment.get("remainingHeadroomTokens"),
        "effective_max_output_tokens": assessment.get("effectiveMaxOutputTokens"),
        "min_useful_output_tokens": assessment.get("minUsefulOutputTokens"),
        "compaction_outcome": assessment.get("compactionOutcome"),
        "compaction_outcome_reason": assessment.get("compactionOutcomeReason"),
    }


def _impossible_headroom_reason(
    *,
    measurement: dict[str, Any],
    target: int,
    fixed: int,
    outcome: dict[str, Any] | None,
) -> str:
    if not measurement.get("source"):
        return "token_measurement_unavailable"
    if fixed >= target > 0:
        return "configured_target_below_irreducible_request"
    outcome_reason = str((outcome or {}).get("reason") or "")
    if outcome_reason == "no_legal_cutoff":
        return "no_legal_tool_boundary"
    if outcome_reason in {
        "already_compacted_no_new_source",
        "insufficient_incremental_relief",
    }:
        return "non_compactable_current_turn_tail"
    if (outcome or {}).get("status") == "completed":
        return "compaction_output_larger_than_projected"
    return "fixed_system_tool_overlay_cost"


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0
