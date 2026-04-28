from __future__ import annotations

from typing import Any


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) == 0:
        raise ValueError(f"Expected {field} to be a non-empty string")
    return value


def _require_number(value: Any, field: str, fallback: float) -> float:
    if value is None:
        return fallback
    if not isinstance(value, (int, float)):
        raise ValueError(f"Expected {field} to be a number")
    return value


def _require_optional_number(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ValueError("Expected number")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Expected {field} to be an array")
    return value


def _without_none_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
