"""Shared primitives for the subagent coordination stores."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from ..storage import with_record_transaction


@asynccontextmanager
async def with_subagent_coordination_transaction(parentSessionId: str) -> AsyncIterator[None]:
    """All subagent coordination stores share one lock domain per parent session."""
    async with with_record_transaction("subagent-coordination", parentSessionId):
        yield


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _dedupe(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
