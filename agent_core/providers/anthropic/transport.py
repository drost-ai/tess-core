from __future__ import annotations

import math

import asyncio
import contextlib
from .._core import (
    _activity_state_bool,
    race_provider_activity as _race_provider_activity,
    raise_if_provider_aborted as _raise_if_provider_aborted,
    signal_aborted as _signal_aborted,
)
from .shared import ANTHROPIC_ABORT_POLL_INTERVAL_MS
from typing import Any


async def _race_anthropic_activity(
    awaitable: Any,
    *,
    timeout_ms: int,
    attempt: int = 1,
    phase: str = "request_start",
    hadAssistantOutput: bool = False,
    hadToolUse: bool = False,
    abortSignal: Any = None,
    activityState: dict[str, Any] | None = None,
    turnSession: Any = None,
) -> Any:
    return await _race_provider_activity(
        awaitable,
        provider="anthropic",
        provider_label="Anthropic",
        message_label="Anthropic",
        timeout_ms=timeout_ms,
        phase=phase,
        attempt=attempt,
        had_output=hadAssistantOutput,
        had_tool_use=hadToolUse,
        abort_signal=abortSignal,
        activity_state=activityState,
        turn_session=turnSession,
        cancel_task=_cancel_anthropic_activity_task,
        abort_poll_interval_ms=ANTHROPIC_ABORT_POLL_INTERVAL_MS,
    )


async def _cancel_anthropic_activity_task(task: asyncio.Future[Any]) -> None:
    owner = getattr(task, "_anthropic_transport_owner", None)
    if owner is not None:
        owner.cancel()
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _raise_anthropic_cancelled(runtime: dict[str, Any], phase: str, attempt: int, hadAssistantOutput: bool, hadToolUse: bool) -> None:
    _raise_if_provider_aborted(
        runtime.get("abortSignal"),
        provider="anthropic",
        provider_label="Anthropic",
        message_label="Anthropic",
        phase=phase,
        attempt=attempt,
        had_output=hadAssistantOutput,
        had_tool_use=hadToolUse,
    )

def _apply_usage_snapshot(target: dict[str, Any], usage: dict[str, Any]) -> None:
    if not isinstance(usage, dict):
        return
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = _finite_usage_number(usage.get(key))
        if value is not None:
            target[key] = value
    creation = usage.get("cache_creation")
    creation = creation if isinstance(creation, dict) else {}
    detail_fields = (
        ("cache_creation_5m_input_tokens", creation.get("ephemeral_5m_input_tokens", usage.get("cache_creation_5m_input_tokens"))),
        ("cache_creation_1h_input_tokens", creation.get("ephemeral_1h_input_tokens", usage.get("cache_creation_1h_input_tokens"))),
    )
    for key, raw in detail_fields:
        value = _finite_usage_number(raw)
        if value is not None:
            target[key] = value
    aggregate = _finite_usage_number(target.get("cache_creation_input_tokens")) or 0
    five_minutes = _finite_usage_number(target.get("cache_creation_5m_input_tokens"))
    one_hour = _finite_usage_number(target.get("cache_creation_1h_input_tokens"))
    if "cache_creation_input_tokens" in target or five_minutes is not None or one_hour is not None:
        target["cache_creation_ttl_detail_complete"] = (
            (five_minutes is not None or one_hour is not None or aggregate == 0)
            and (five_minutes or 0) + (one_hour or 0) == aggregate
        )


def _finite_usage_number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


applyAnthropicUsageSnapshot = _apply_usage_snapshot
