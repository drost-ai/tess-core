from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any


def boundDiagnosticText(value: str, maximum: int) -> str:
    normalized = re.sub(r"[\r\n\t]+", " ", value).strip()
    return (
        normalized
        if len(normalized) <= maximum
        else f"{normalized[: maximum - 1]}…"
    )


def boundOptionalDiagnosticText(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    text = boundDiagnosticText(str(value), maximum)
    return text or None


def validHttpStatus(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def positiveOptionalInteger(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return min(value, 9_007_199_254_740_991)


def nonnegativeOptionalInteger(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, 9_007_199_254_740_991)


def partialOutputMode(value: Any) -> str | None:
    return value if value in {"buffered", "replaceable", "append_only"} else None


def boundedInterruptedPreview(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "type": "assistant_interrupted_preview",
        "logicalCallId": boundOptionalDiagnosticText(
            value.get("logicalCallId"), 128
        ),
        "generation": positiveOptionalInteger(value.get("generation")),
        "deliveryMode": partialOutputMode(value.get("deliveryMode")),
        "textHash": boundOptionalDiagnosticText(value.get("textHash"), 128),
        "textLength": nonnegativeOptionalInteger(value.get("textLength")),
        "reasoningTextHash": boundOptionalDiagnosticText(
            value.get("reasoningTextHash"), 128
        ),
        "reasoningTextLength": nonnegativeOptionalInteger(
            value.get("reasoningTextLength")
        ),
        "deliveryAcknowledged": bool(value.get("deliveryAcknowledged")),
        "reason": boundOptionalDiagnosticText(value.get("reason"), 256),
    }


def boundDiagnosticCount(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return 0
    return min(9_007_199_254_740_991, int(value))
