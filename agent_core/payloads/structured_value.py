from __future__ import annotations

import json
import math
from typing import Any

from .utf8_bounds import slice_utf8_prefix, utf8_byte_length


TRUNCATED_MARKER = "[truncated]"


def summarize_structured_value(source: Any, limits: dict[str, Any] | None = None) -> dict[str, Any]:
    limits = limits or {}
    policy = {
        "maxDepth": _normalize_limit(limits.get("maxDepth"), 5),
        "maxKeys": _normalize_limit(limits.get("maxKeys"), 40),
        "maxItems": _normalize_limit(limits.get("maxItems"), 60),
        "maxStringBytes": _normalize_limit(limits.get("maxStringBytes"), 2_000),
        "maxBytes": _normalize_limit(limits.get("maxBytes"), 8_000),
    }
    state: dict[str, Any] = {
        "remainingBytes": policy["maxBytes"],
        "remainingItems": policy["maxItems"],
        "visitedItems": 0,
        "returnedItems": 0,
        "omittedItems": 0,
        "truncated": False,
        "seen": set(),
    }

    def visit(value: Any, depth: int) -> Any:
        state["visitedItems"] += 1
        if state["remainingItems"] <= 0 or state["remainingBytes"] <= 0:
            state["omittedItems"] += 1
            state["truncated"] = True
            return TRUNCATED_MARKER
        state["remainingItems"] -= 1

        if value is None or isinstance(value, bool):
            _consume(state, 4 if value is None or value else 5)
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            normalized: Any = value if not isinstance(value, float) or math.isfinite(value) else str(value)
            _consume(state, utf8_byte_length(str(normalized)))
            return normalized
        if isinstance(value, str):
            max_bytes = min(policy["maxStringBytes"], max(0, state["remainingBytes"]))
            fitted = slice_utf8_prefix(value, max_bytes)
            if fitted != value:
                state["truncated"] = True
            _consume(state, utf8_byte_length(fitted) + 2)
            return fitted
        if depth >= policy["maxDepth"]:
            state["truncated"] = True
            state["omittedItems"] += 1
            return "[max depth]"
        if isinstance(value, (dict, list, tuple)):
            identity = id(value)
            if identity in state["seen"]:
                state["truncated"] = True
                return "[circular]"
            state["seen"].add(identity)
        else:
            state["truncated"] = True
            return f"[{type(value).__name__}]"

        if isinstance(value, (list, tuple)):
            output: list[Any] = []
            for index, item in enumerate(value):
                if state["remainingItems"] <= 0 or state["remainingBytes"] <= 16:
                    state["omittedItems"] += len(value) - index
                    state["truncated"] = True
                    break
                state["returnedItems"] += 1
                output.append(visit(item, depth + 1))
            if len(output) < len(value):
                output.append({"omittedItems": len(value) - len(output)})
            _consume(state, 2 + max(0, len(output) - 1))
            return output

        selected_keys = list(value.keys())[: policy["maxKeys"]]
        has_additional = len(value) > len(selected_keys)
        if has_additional:
            state["omittedItems"] += 1
            state["truncated"] = True
        output_dict: dict[str, Any] = {}
        processed = 0
        for raw_key in selected_keys:
            if state["remainingItems"] <= 0 or state["remainingBytes"] <= 16:
                state["omittedItems"] += len(selected_keys) - processed
                state["truncated"] = True
                break
            key = _unique_summary_key(
                output_dict,
                slice_utf8_prefix(str(raw_key), min(256, state["remainingBytes"])),
            )
            _consume(state, utf8_byte_length(key) + 3)
            state["returnedItems"] += 1
            processed += 1
            try:
                output_dict[key] = visit(value[raw_key], depth + 1)
            except Exception:
                output_dict[key] = "[unreadable value]"
                state["truncated"] = True
        if has_additional:
            output_dict[_unique_summary_key(output_dict, "__omittedKeys")] = "at least 1"
        _consume(state, 2 + max(0, processed - 1))
        return output_dict

    value = visit(source, 0)
    serialized = _safe_stringify(value)
    if utf8_byte_length(serialized) > policy["maxBytes"]:
        state["truncated"] = True
        value = _fit_serialized_summary(serialized, policy["maxBytes"])
        serialized = _safe_stringify(value)
    return {
        "value": value,
        "truncated": state["truncated"],
        "visitedItems": state["visitedItems"],
        "returnedItems": state["returnedItems"],
        "omittedItems": state["omittedItems"],
        "returnedBytes": utf8_byte_length(serialized),
    }


def _unique_summary_key(output: dict[str, Any], preferred: str) -> str:
    base = preferred or "[empty key]"
    if base not in output:
        return base
    suffix = 2
    while f"{base}#{suffix}" in output:
        suffix += 1
    return f"{base}#{suffix}"


def _fit_serialized_summary(source: str, max_bytes: int) -> dict[str, Any]:
    low = 0
    high = min(utf8_byte_length(source), max_bytes)
    best: dict[str, Any] = {"truncated": True}
    while low <= high:
        midpoint = (low + high) // 2
        candidate = {"summary": slice_utf8_prefix(source, midpoint), "truncated": True}
        if utf8_byte_length(_safe_stringify(candidate)) <= max_bytes:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _safe_stringify(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return '{"summary":"[unserializable]","truncated":true}'


def _consume(state: dict[str, Any], size: int) -> None:
    state["remainingBytes"] = max(0, state["remainingBytes"] - max(0, size))


def _normalize_limit(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return fallback
    return math.floor(value)


