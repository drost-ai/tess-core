"""Shared coercion helpers for context-package modules."""
from __future__ import annotations

from typing import Any


def as_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_string_array(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, (str, int, float)) and str(item).strip()]
