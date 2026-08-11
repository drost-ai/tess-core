from __future__ import annotations

from typing import Any

from .shared import _field


def reduce_compaction_headroom(
    current: dict[str, Any],
    data: dict[str, Any],
    timestamp: Any,
) -> dict[str, Any]:
    return {
        **current,
        "headroom": {
            "status": _field(data, "status"),
            "reason": _field(data, "reason"),
            "inputTokens": _field(data, "inputTokens", "input_tokens"),
            "irreducibleInputTokens": _field(
                data,
                "irreducibleInputTokens",
                "irreducible_input_tokens",
            ),
            "compactionThresholdTokens": _field(
                data,
                "compactionThresholdTokens",
                "compaction_threshold_tokens",
            ),
            "compactionTargetTokens": _field(
                data,
                "compactionTargetTokens",
                "compaction_target_tokens",
            ),
            "remainingHeadroomTokens": _field(
                data,
                "remainingHeadroomTokens",
                "remaining_headroom_tokens",
            ),
            "updatedAt": timestamp,
        },
        "updatedAt": timestamp,
    }


__all__ = ["reduce_compaction_headroom"]
