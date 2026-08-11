from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any


def bound_diagnostic_text(value: str, maximum: int) -> str:
    normalized = re.sub(r"[\r\n\t]+", " ", value).strip()
    return (
        normalized
        if len(normalized) <= maximum
        else f"{normalized[: maximum - 1]}…"
    )


def bound_optional_diagnostic_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    text = bound_diagnostic_text(str(value), maximum)
    return text or None


def valid_http_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def positive_optional_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return min(value, 9_007_199_254_740_991)


def nonnegative_optional_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, 9_007_199_254_740_991)


def partial_output_mode(value: Any) -> str | None:
    return value if value in {"buffered", "replaceable", "append_only"} else None


def bounded_interrupted_preview(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "type": "assistant_interrupted_preview",
        "logicalCallId": bound_optional_diagnostic_text(
            value.get("logicalCallId"), 128
        ),
        "generation": positive_optional_integer(value.get("generation")),
        "deliveryMode": partial_output_mode(value.get("deliveryMode")),
        "textHash": bound_optional_diagnostic_text(value.get("textHash"), 128),
        "textLength": nonnegative_optional_integer(value.get("textLength")),
        "reasoningTextHash": bound_optional_diagnostic_text(
            value.get("reasoningTextHash"), 128
        ),
        "reasoningTextLength": nonnegative_optional_integer(
            value.get("reasoningTextLength")
        ),
        "deliveryAcknowledged": bool(value.get("deliveryAcknowledged")),
        "reason": bound_optional_diagnostic_text(value.get("reason"), 256),
    }


def bound_diagnostic_count(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return 0
    return min(9_007_199_254_740_991, int(value))


def looks_like_auth(message: str) -> bool:
    return bool(re.search(r"unauthorized|forbidden|invalid api key|authentication|auth token|insufficient permissions?|missing scopes?|insufficient_scope|permission denied", message))


def looks_like_quota(message: str) -> bool:
    return bool(re.search(r"quota|credit|billing|insufficient funds|payment required|usage limit|out of extra usage", message))


def looks_like_timeout(message: str) -> bool:
    return bool(re.search(r"timed out|timeout|etimedout|connect timeout|headers timeout|body timeout|deadline exceeded", message))


def looks_like_transport(message: str) -> bool:
    return bool(re.search(r"terminated|socket hang up|econnreset|econnrefused|enotfound|network|fetch failed|connection closed|connection without response|connection error|apiconnectionerror|remote end closed|other side closed|premature close|broken pipe", message))
