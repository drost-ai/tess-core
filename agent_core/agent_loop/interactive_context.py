from __future__ import annotations

from typing import Any, NoReturn

from ..provider_errors import createProviderFailureRecord, createProviderRequestError
from ..provider_token_budget import resolve_interactive_context_policy


def planInteractiveContextRelief(
    *,
    config: dict[str, Any],
    measurement: dict[str, Any],
    currentMessageThreshold: int | None,
) -> dict[str, Any]:
    policy = resolve_interactive_context_policy(
        config.get("providerProfile") or {}
    )
    threshold = policy.get("contextReliefThresholdTokens")
    target = policy.get("contextReliefTargetTokens")
    input_tokens = int(measurement.get("inputTokens") or 0)
    if not threshold or not target:
        return {"action": "ready", "policy": policy}
    if currentMessageThreshold is None and input_tokens > int(threshold):
        effective_fixed = max(
            0,
            input_tokens - int(measurement.get("estimatedMessageInputTokens") or 0),
        )
        if effective_fixed >= int(target):
            _raise_failure(
                config=config,
                measurement=measurement,
                policy=policy,
                reason="configured_target_below_irreducible_request",
            )
        return {
            "action": "project",
            "policy": policy,
            "messageThreshold": max(1, int(threshold) - effective_fixed),
            "messageTarget": max(1, int(target) - effective_fixed - 1_024),
        }
    if currentMessageThreshold is not None and input_tokens > int(target):
        _raise_failure(
            config=config,
            measurement=measurement,
            policy=policy,
            reason="safe_projection_did_not_reach_target",
        )
    return {"action": "ready", "policy": policy}


def interactiveReliefReceipt(
    *,
    policy: dict[str, Any],
    originalInputTokens: int | None,
    projectedInputTokens: int,
    applied: bool,
) -> dict[str, Any]:
    target = policy.get("contextReliefTargetTokens")
    return {
        "applied": applied,
        "source": policy.get("reliefSource"),
        "thresholdTokens": policy.get("contextReliefThresholdTokens"),
        "targetTokens": target,
        "originalInputTokens": originalInputTokens,
        "projectedInputTokens": projectedInputTokens,
        "targetMet": target is None or projectedInputTokens <= int(target),
    }


def raiseInteractiveContextConvergenceFailure(
    *,
    config: dict[str, Any],
    measurement: dict[str, Any],
) -> NoReturn:
    policy = resolve_interactive_context_policy(
        config.get("providerProfile") or {}
    )
    _raise_failure(
        config=config,
        measurement=measurement,
        policy=policy,
        reason="request_preparation_retry_exhausted",
    )


def _raise_failure(
    *,
    config: dict[str, Any],
    measurement: dict[str, Any],
    policy: dict[str, Any],
    reason: str,
) -> None:
    raise createProviderRequestError(
        createProviderFailureRecord(
            provider=config.get("provider"),
            providerLabel=config.get("providerProfileName")
            or config.get("provider"),
            phase="request_build",
            category="client_request",
            retryable=False,
            attempt=1,
            hadAssistantOutput=False,
            hadToolUse=False,
            code="interactive_context_relief_failed",
            rawMessage=(
                "Provider dispatch blocked because the interactive request "
                f"could not be projected to its configured target ({reason}; "
                f"input={measurement.get('inputTokens')}, "
                f"fixed={measurement.get('estimatedFixedInputTokens')}, "
                f"target={policy.get('contextReliefTargetTokens')}, "
                f"threshold={policy.get('contextReliefThresholdTokens')})."
            ),
        )
    )
