from __future__ import annotations

from typing import Any

from ..providers.token_budget import resolve_interactive_context_policy


def retiredInteractiveReliefReceipt(
    *,
    config: dict[str, Any],
    measuredInputTokens: int,
) -> dict[str, Any]:
    resolved = resolve_interactive_context_policy(
        config.get("providerProfile") or {}
    )
    return {
        "applied": False,
        "source": resolved.get("reliefSource"),
        "thresholdTokens": None,
        "targetTokens": None,
        "originalInputTokens": None,
        "projectedInputTokens": measuredInputTokens,
        "targetMet": True,
    }
