from __future__ import annotations

import copy
from typing import Any


def interactiveReliefProjection(
    continuation: dict[str, Any],
    measured_input: int,
) -> dict[str, Any]:
    relief = continuation.get("interactiveRelief")
    if isinstance(relief, dict):
        return copy.deepcopy(relief)
    return {
        "applied": False,
        "source": "disabled",
        "thresholdTokens": None,
        "targetTokens": None,
        "originalInputTokens": None,
        "projectedInputTokens": measured_input,
        "targetMet": True,
    }
