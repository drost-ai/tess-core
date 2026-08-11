"""Shared provider-core primitives.

One abort/signal set, one activity-extending BaseException-safe race, one
timeout/cancel record builder, and one retry-delay policy (ladder + bounded
jitter + server Retry-After). Provider modules keep thin, patchable wrappers
around these; the policy itself lives here exactly once.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .._utils import signal_aborted
from .errors import (
    ProviderRequestError,
    create_provider_failure_record,
    create_provider_request_error,
    retry_delay_from_ladder,
)

DEFAULT_ABORT_POLL_INTERVAL_MS = 50


def finite_nonnegative_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def finite_nonnegative_int(value: Any) -> int:
    return finite_nonnegative_int_or_none(value) or 0


def is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
RETRY_JITTER_MS = 119
RETRY_AFTER_CAP_MS = 60_000


class AbortError(RuntimeError):
    def __init__(self, message: str = "Request cancelled"):
        super().__init__(message)
        self.name = "AbortError"


class AbortSignal:
    def __init__(self) -> None:
        self.aborted = False
        self.reason: BaseException | None = None
        self.event = asyncio.Event()

    def abort(self, reason: BaseException | None = None) -> None:
        if self.aborted:
            return
        self.aborted = True
        self.reason = reason or AbortError()
        self.event.set()

    async def wait(self) -> None:
        await self.event.wait()


def signal_reason(signal: Any) -> BaseException:
    reason = signal.get("reason") if isinstance(signal, dict) else getattr(signal, "reason", None)
    return reason if isinstance(reason, BaseException) else AbortError()


async def wait_for_abort(
    signal: Any, *, poll_interval_ms: int = DEFAULT_ABORT_POLL_INTERVAL_MS
) -> None:
    """Resolve when the signal aborts; wait forever on a None signal."""
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
    while not signal_aborted(signal):
        await asyncio.sleep(max(1, poll_interval_ms) / 1000)


def create_provider_cancelled_error(
    *,
    provider: str,
    provider_label: str,
    message_label: str,
    phase: str,
    attempt: int,
    had_output: bool,
    had_tool_use: bool,
) -> ProviderRequestError:
    return create_provider_request_error(
        create_provider_failure_record(
            provider=provider,
            providerLabel=provider_label,
            phase=phase,
            category="cancelled",
            retryable=False,
            attempt=attempt,
            hadAssistantOutput=had_output,
            hadToolUse=had_tool_use,
            rawMessage=f"{message_label} request was cancelled.",
        )
    )


def create_provider_timeout_error(
    *,
    provider: str,
    provider_label: str,
    message_label: str,
    phase: str,
    attempt: int,
    had_output: bool,
    had_tool_use: bool,
    timeout_ms: int,
) -> ProviderRequestError:
    # record.retryable describes the fault class only. Whether partial output
    # blocks a re-run is the retry/fallback decision's call — those sites know
    # the sink capabilities (a replaceable codex sink may supersede partial
    # output; plain modes and fallback never re-run after output).
    return create_provider_request_error(
        create_provider_failure_record(
            provider=provider,
            providerLabel=provider_label,
            phase=phase,
            category="timeout",
            retryable=True,
            attempt=attempt,
            hadAssistantOutput=had_output,
            hadToolUse=had_tool_use,
            code=(
                "provider_request_start_timeout"
                if phase == "request_start"
                else "provider_stream_idle_timeout"
            ),
            rawMessage=(
                f"{message_label} request timed out after {timeout_ms}ms "
                "while waiting for provider activity."
            ),
        )
    )


def raise_if_provider_aborted(
    signal: Any,
    *,
    provider: str,
    provider_label: str,
    message_label: str,
    phase: str,
    attempt: int,
    had_output: bool,
    had_tool_use: bool,
) -> None:
    if not signal_aborted(signal):
        return
    raise create_provider_cancelled_error(
        provider=provider,
        provider_label=provider_label,
        message_label=message_label,
        phase=phase,
        attempt=attempt,
        had_output=had_output,
        had_tool_use=had_tool_use,
    )


async def race_provider_activity(
    awaitable: Any,
    *,
    provider: str,
    provider_label: str,
    message_label: str,
    timeout_ms: int,
    phase: str = "request_start",
    attempt: int = 1,
    had_output: bool = False,
    had_tool_use: bool = False,
    abort_signal: Any = None,
    activity_state: dict[str, Any] | None = None,
    turn_session: Any = None,
    cancel_task: Callable[[asyncio.Future[Any]], Awaitable[None]] | None = None,
    abort_poll_interval_ms: int = DEFAULT_ABORT_POLL_INTERVAL_MS,
) -> Any:
    """Race provider activity against abort and an activity-extending deadline.

    The single race primitive: event-driven abort, deadline extension from
    ``activity_state['last_activity']``, and a BaseException guard so outer
    cancellation can never orphan the in-flight task. The turn session's
    activity task binding is cleared on every exit path.
    """

    def cancelled_error() -> ProviderRequestError:
        return create_provider_cancelled_error(
            provider=provider,
            provider_label=provider_label,
            message_label=message_label,
            phase=phase,
            attempt=attempt,
            had_output=had_output,
            had_tool_use=had_tool_use,
        )

    if signal_aborted(abort_signal):
        raise cancelled_error()
    task = asyncio.ensure_future(awaitable)
    if turn_session is not None:
        turn_session.bind_activity_task(task)
    canceller = cancel_task or _cancel_activity_task
    timeout_seconds = max(1, timeout_ms) / 1000
    deadline = time.monotonic() + timeout_seconds
    abort_waiter = asyncio.create_task(
        wait_for_abort(abort_signal, poll_interval_ms=abort_poll_interval_ms)
    )
    activity_event = (
        activity_state.get("activity_event") if isinstance(activity_state, dict) else None
    )
    try:
        while True:
            if task.done():
                return task.result()
            if signal_aborted(abort_signal):
                await canceller(task)
                raise cancelled_error()
            if activity_state is not None:
                last_activity = activity_state.get("last_activity")
                if isinstance(last_activity, (int, float)):
                    deadline = float(last_activity) + timeout_seconds
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await canceller(task)
                if signal_aborted(abort_signal):
                    raise cancelled_error()
                raise create_provider_timeout_error(
                    provider=provider,
                    provider_label=provider_label,
                    message_label=message_label,
                    phase=(
                        "stream_read"
                        if activity_state is not None
                        and activity_state.get("saw_activity") is True
                        else phase
                    ),
                    attempt=attempt,
                    had_output=_activity_state_bool(
                        activity_state, "had_assistant_output", had_output
                    ),
                    had_tool_use=_activity_state_bool(
                        activity_state, "had_tool_use", had_tool_use
                    ),
                    timeout_ms=timeout_ms,
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
                await canceller(task)
                raise cancelled_error()
            if activity_waiter is not None and activity_waiter in done:
                activity_event.clear()
    except BaseException:
        if not task.done():
            await canceller(task)
        raise
    finally:
        if not abort_waiter.done():
            abort_waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await abort_waiter
        if turn_session is not None:
            turn_session.clear_activity_task(task)


def retry_after_ms_from_headers(headers: Any) -> int | None:
    """Parse a Retry-After header (numeric-seconds form) into milliseconds."""
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    raw = getter("retry-after") or getter("Retry-After")
    if raw is None:
        return None
    token = str(raw).split(",")[0].strip()
    try:
        seconds = float(token)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return int(seconds * 1000)


def provider_retry_delay_ms(
    attempt: int, *, ladder: tuple[int, ...], failure: Any = None
) -> int:
    """The retry-delay policy: ladder + server Retry-After + bounded jitter.

    Server guidance can only extend the ladder delay, never shrink it, and is
    capped so a hostile header cannot park a turn for minutes.
    """
    base = retry_delay_from_ladder(ladder, attempt)
    record = getattr(failure, "record", None)
    retry_after = record.get("retryAfterMs") if isinstance(record, dict) else None
    if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after > 0:
        base = max(base, min(retry_after, RETRY_AFTER_CAP_MS))
    return base + random.randint(0, RETRY_JITTER_MS)


async def _cancel_activity_task(task: asyncio.Future[Any]) -> None:
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _activity_state_bool(
    activity_state: dict[str, Any] | None, key: str, fallback: bool
) -> bool:
    if not isinstance(activity_state, dict):
        return fallback
    value = activity_state.get(key)
    return bool(value) if value is not None else fallback
