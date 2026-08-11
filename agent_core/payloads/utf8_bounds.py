from __future__ import annotations

import math
from typing import Any


REPLACEMENT_CHARACTER = "\ufffd"


def utf8_byte_length(value: str) -> int:
    return len(normalize_utf8_string(value).encode("utf-8"))


def normalize_utf8_string(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Expected a string")
    if not has_lone_utf16_surrogate(value):
        return value
    return "".join(
        REPLACEMENT_CHARACTER if 0xD800 <= ord(chunk) <= 0xDFFF else chunk
        for chunk in value
    )


def slice_utf8_prefix(value: str, maxBytes: int | float) -> str:
    limit = _normalize_limit(maxBytes)
    if limit == 0:
        return ""
    output: list[str] = []
    used = 0
    for chunk in normalize_utf8_string(value):
        size = len(chunk.encode("utf-8"))
        if used + size > limit:
            break
        output.append(chunk)
        used += size
    return "".join(output)


def slice_utf8_suffix(value: str, maxBytes: int | float) -> str:
    limit = _normalize_limit(maxBytes)
    if limit == 0:
        return ""
    output: list[str] = []
    used = 0
    for chunk in reversed(normalize_utf8_string(value)):
        size = len(chunk.encode("utf-8"))
        if used + size > limit:
            break
        output.append(chunk)
        used += size
    output.reverse()
    return "".join(output)


def slice_utf8_window(value: str, startByteOffset: int, maxBytes: int | float) -> dict[str, Any]:
    normalized = normalize_utf8_string(value)
    source_bytes = utf8_byte_length(normalized)
    start = _normalize_byte_offset(startByteOffset)
    if start > source_bytes:
        raise ValueError(f"UTF-8 byte offset {start} exceeds source size {source_bytes}.")
    limit = _normalize_limit(maxBytes)
    selected: list[str] = []
    position = 0
    returned = 0
    skipped = 0
    for chunk in normalized:
        chunk_bytes = len(chunk.encode("utf-8"))
        next_position = position + chunk_bytes
        if next_position <= start:
            position = next_position
            continue
        if position < start:
            raise ValueError(f"UTF-8 byte offset {start} is not on a code-point boundary.")
        if returned + chunk_bytes > limit:
            if returned == 0:
                skipped = chunk_bytes
            break
        selected.append(chunk)
        returned += chunk_bytes
        position = next_position
    advanced = returned + skipped
    result: dict[str, Any] = {
        "value": "".join(selected),
        "sourceBytes": source_bytes,
        "startByteOffset": start,
        "returnedBytes": returned,
        "skippedBytes": skipped,
    }
    if start + advanced < source_bytes:
        result["nextByteOffset"] = start + advanced
    return result


def truncate_utf8(value: str, maxBytes: int | float, marker: str = "…") -> dict[str, Any]:
    normalized = normalize_utf8_string(value)
    limit = _normalize_limit(maxBytes)
    if utf8_byte_length(normalized) <= limit:
        return {"value": normalized, "truncated": normalized != value}
    fitted_marker = slice_utf8_prefix(normalize_utf8_string(marker), limit)
    prefix = slice_utf8_prefix(normalized, max(0, limit - utf8_byte_length(fitted_marker)))
    return {"value": f"{prefix}{fitted_marker}", "truncated": True}


def build_utf8_head_tail_sample(value: str, maxBytes: int | float) -> str:
    normalized = normalize_utf8_string(value)
    limit = _normalize_limit(maxBytes)
    if utf8_byte_length(normalized) <= limit:
        return normalized
    separator = slice_utf8_prefix("\n…[truncated]…\n", limit)
    content_budget = max(0, limit - utf8_byte_length(separator))
    head_budget = math.floor(content_budget * 0.45)
    return f"{slice_utf8_prefix(normalized, head_budget)}{separator}{slice_utf8_suffix(normalized, content_budget - head_budget)}"


def has_lone_utf16_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(chunk) <= 0xDFFF for chunk in value)


def assert_utf8_byte_limit(value: str, maxBytes: int | float, label: str) -> None:
    raw_bytes = len(value.encode("utf-8", errors="replace"))
    if raw_bytes > _normalize_limit(maxBytes):
        raise ValueError(f"{label} is {raw_bytes} UTF-8 bytes; limit is {_normalize_limit(maxBytes)}.")
    if has_lone_utf16_surrogate(value):
        raise ValueError(f"{label} contains a lone UTF-16 surrogate.")


def _normalize_limit(value: int | float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return max(0, math.floor(value))


def _normalize_byte_offset(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("UTF-8 byte offset must be a non-negative safe integer.")
    return value


