from __future__ import annotations

from .shared import *


def _read_output_preview(output_file: str | None, tail_bytes: int = 512) -> str | None:
    if not output_file:
        return None
    try:
        path = Path(output_file)
        size = path.stat().st_size
        if size <= 0:
            return None
        bytes_to_read = min(size, max(64, tail_bytes))
        with path.open("rb") as handle:
            handle.seek(max(0, size - bytes_to_read))
            data = handle.read(bytes_to_read)
        lines = [_collapse_whitespace(line) for line in data.decode("utf-8", errors="replace").splitlines()]
        preview = next((line for line in reversed(lines) if line), None)
        return _truncate(preview, 180) if preview else None
    except Exception:
        return None


def _latest_timestamp(left: Any, right: Any) -> str:
    if not isinstance(left, str) or not left:
        return str(right or now_iso())
    if not isinstance(right, str) or not right:
        return left
    left_ms = _parse_iso_ms(left)
    right_ms = _parse_iso_ms(right)
    if left_ms is None or right_ms is None:
        return left if left >= right else right
    return left if left_ms >= right_ms else right


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max(0, max_length - 3)].rstrip()}..."


def _parse_iso_ms(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    import datetime

    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def _bucket_ms(value: float) -> int:
    import math

    if value <= 5000:
        return int(math.ceil(value / 1000) * 1000)
    return int(math.ceil(value / 5000) * 5000)


def _summarize_goal(goal: Any) -> str:
    text = str(goal or "").strip()
    return text if len(text) <= 48 else f"{text[:45].rstrip()}..."


def _output_size(output_file: str | None) -> int:
    return Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0


def _dedupe(values: list[Any]) -> list[Any]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected {field} to be a non-empty string")
    return value
