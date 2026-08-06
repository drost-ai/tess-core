from __future__ import annotations

import math
import time

from ..anthropic_http_transport import (
    cancelAnthropicTransportFuture as _cancel_anthropic_transport_future,
)

from .shared import *

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
    _raise_anthropic_cancelled({"abortSignal": abortSignal}, phase, attempt, hadAssistantOutput, hadToolUse)
    task = asyncio.ensure_future(awaitable)
    if turnSession is not None:
        turnSession.bind_activity_task(task)
    timeout_seconds = max(1, timeout_ms) / 1000
    deadline = time.monotonic() + timeout_seconds
    abort_waiter = asyncio.create_task(_wait_for_anthropic_abort(abortSignal))
    activity_event = (
        activityState.get("activity_event")
        if isinstance(activityState, dict)
        else None
    )
    try:
        while True:
            if task.done():
                return task.result()
            if _signal_aborted(abortSignal):
                await _cancel_anthropic_activity_task(task)
                _raise_anthropic_cancelled({"abortSignal": abortSignal}, phase, attempt, hadAssistantOutput, hadToolUse)
            if activityState is not None:
                last_activity = activityState.get("last_activity")
                if isinstance(last_activity, (int, float)):
                    deadline = float(last_activity) + timeout_seconds
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await _cancel_anthropic_activity_task(task)
                _raise_anthropic_cancelled({"abortSignal": abortSignal}, phase, attempt, hadAssistantOutput, hadToolUse)
                raise _create_anthropic_timeout(
                    (
                        "stream_read"
                        if activityState is not None
                        and activityState.get("saw_activity") is True
                        else phase
                    ),
                    attempt,
                    _activity_state_bool(activityState, "had_assistant_output", hadAssistantOutput),
                    _activity_state_bool(activityState, "had_tool_use", hadToolUse),
                    timeout_ms,
                )
            deadline_waiter = asyncio.create_task(asyncio.sleep(remaining))
            activity_waiter = (
                asyncio.create_task(activity_event.wait())
                if isinstance(activity_event, asyncio.Event)
                else None
            )
            waiters = {task, abort_waiter, deadline_waiter}
            if activity_waiter is not None:
                waiters.add(activity_waiter)
            try:
                done, _pending = await asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                transient = [
                    waiter
                    for waiter in (deadline_waiter, activity_waiter)
                    if waiter is not None
                ]
                for waiter in transient:
                    if not waiter.done():
                        waiter.cancel()
                if transient:
                    await asyncio.gather(*transient, return_exceptions=True)
            if task in done:
                return task.result()
            if abort_waiter in done:
                await _cancel_anthropic_activity_task(task)
                _raise_anthropic_cancelled({"abortSignal": abortSignal}, phase, attempt, hadAssistantOutput, hadToolUse)
            if activity_waiter is not None and activity_waiter in done:
                activity_event.clear()
            if deadline_waiter in done:
                continue
    except BaseException:
        if not task.done():
            await _cancel_anthropic_activity_task(task)
        raise
    finally:
        if not abort_waiter.done():
            abort_waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await abort_waiter
        if turnSession is not None:
            turnSession.clear_activity_task(task)


def _activity_state_bool(activityState: dict[str, Any] | None, key: str, fallback: bool) -> bool:
    if not isinstance(activityState, dict):
        return fallback
    value = activityState.get(key)
    return bool(value) if value is not None else fallback


def _create_anthropic_timeout(phase: str, attempt: int, hadAssistantOutput: bool, hadToolUse: bool, timeout_ms: int) -> Any:
    code = (
        "provider_request_start_timeout"
        if phase == "request_start"
        else "provider_stream_idle_timeout"
    )
    return createProviderRequestError(
        createProviderFailureRecord(
            provider="anthropic",
            providerLabel="Anthropic",
            phase=phase,
            category="timeout",
            retryable=not hadAssistantOutput and not hadToolUse,
            attempt=attempt,
            hadAssistantOutput=hadAssistantOutput,
            hadToolUse=hadToolUse,
            code=code,
            rawMessage=f"Anthropic request timed out after {timeout_ms}ms while waiting for provider activity.",
        )
    )

async def _cancel_anthropic_activity_task(task: asyncio.Future[Any]) -> None:
    await _cancel_anthropic_transport_future(task)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _wait_for_anthropic_abort(signal: Any) -> None:
    if signal is None:
        await asyncio.Event().wait()
        return
    waiter = getattr(signal, "wait", None)
    if callable(waiter):
        await waiter()
        return
    event = signal.get("event") if isinstance(signal, dict) else getattr(signal, "event", None)
    if isinstance(event, asyncio.Event):
        await event.wait()
        return
    while not _signal_aborted(signal):
        await asyncio.sleep(ANTHROPIC_ABORT_POLL_INTERVAL_MS / 1000)

def _raise_anthropic_cancelled(runtime: dict[str, Any], phase: str, attempt: int, hadAssistantOutput: bool, hadToolUse: bool) -> None:
    if not _signal_aborted(runtime.get("abortSignal")):
        return
    raise createProviderRequestError(
        createProviderFailureRecord(
            provider="anthropic",
            providerLabel="Anthropic",
            phase=phase,
            category="cancelled",
            retryable=False,
            attempt=attempt,
            hadAssistantOutput=hadAssistantOutput,
            hadToolUse=hadToolUse,
            rawMessage="Anthropic request was cancelled.",
        )
    )

def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))

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
