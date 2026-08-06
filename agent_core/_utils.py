from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable

from .providers.usage_costs import mergeProviderUsage as _merge_provider_usage


MISSING = object()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def get_value(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def set_value(obj: Any, key: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[key] = value
    else:
        setattr(obj, key, value)


def ensure_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, SimpleNamespace):
        return vars(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def call_maybe_async(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await maybe_await(fn(*args, **kwargs))


async def to_async_iter(value: Any):
    value = await maybe_await(value)
    if hasattr(value, "__aiter__"):
        async for item in value:
            yield item
        return
    if hasattr(value, "__iter__"):
        for item in value:
            yield item
        return
    raise TypeError("Expected an async iterable or iterable response")


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def merge_usage(target: dict[str, Any], next_usage: dict[str, Any]) -> None:
    _merge_provider_usage(target, next_usage)


def truthy_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def flatten(values: Iterable[Iterable[Any]]) -> list[Any]:
    out: list[Any] = []
    for value in values:
        out.extend(value)
    return out


def field(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data.get(name)
    return default


def safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def parse_iso_ms(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def is_process_alive(pid: Any) -> bool:
    """Signal-0 liveness probe with strict pid validation.

    PermissionError means the pid exists but belongs to another user - that
    is *alive*; reporting it dead lets reconciliation fail a running task.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
