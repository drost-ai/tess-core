from __future__ import annotations

from collections.abc import Callable
from typing import Any


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(val) for key, val in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Callable):
        return "<callable>"
    return repr(value)


def selected_headers(headers: dict[str, Any]) -> dict[str, str]:
    keep = {
        "accept",
        "anthropic-beta",
        "anthropic-version",
        "authorization",
        "content-type",
        "user-agent",
        "x-api-key",
        "x-app",
    }
    return {str(key).lower(): str(value) for key, value in headers.items() if str(key).lower() in keep}


def get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
            continue
        return None
    return current
