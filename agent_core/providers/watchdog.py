from __future__ import annotations

import asyncio
import contextlib as _contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ._core import (
    AbortError,
    AbortSignal,
    signal_aborted as _signal_aborted,
    signal_reason as _signal_reason,
    wait_for_abort as _wait_for_signal,
)


class ProviderRequestWatchdog:
    def __init__(
        self,
        *,
        parentSignal: Any = None,
        initialTimeoutMs: int,
        buildTimeoutError: Callable[[], BaseException],
    ) -> None:
        self._parent_signal = parentSignal
        self._initial_timeout_ms = max(1, int(initialTimeoutMs))
        self._build_timeout_error = buildTimeoutError
        self._deadline = 0.0
        self._timed_out = False
        self._timeout_error: BaseException | None = None
        self._disposed = False
        self._deadline_changed = asyncio.Event()
        self._deadline_generation = 0
        self.signal = AbortSignal()
        self.reset(self._initial_timeout_ms)
        if _signal_aborted(parentSignal):
            self.signal.abort(_signal_reason(parentSignal))

    def didTimeout(self) -> bool:
        self._refresh_timeout()
        return self._timed_out

    def reset(self, timeoutMs: int | None = None) -> None:
        self._timed_out = False
        self._timeout_error = None
        self._deadline = time.monotonic() + (max(1, int(timeoutMs or self._initial_timeout_ms)) / 1000)
        self._deadline_generation += 1
        self._deadline_changed.set()

    async def race(self, awaitable: Awaitable[Any]) -> Any:
        task = asyncio.ensure_future(awaitable)
        # JavaScript constructs and starts the provider Promise before
        # Promise.race observes an already-fired watchdog. Give the Python
        # coroutine the same single scheduling turn before checking deadlines.
        await asyncio.sleep(0)
        parent_waiter = asyncio.create_task(_wait_for_signal(self._parent_signal))
        signal_waiter = asyncio.create_task(self.signal.wait())
        try:
            while True:
                if task.done():
                    try:
                        return task.result()
                    except BaseException:
                        self._refresh_timeout()
                        if self._timed_out:
                            raise self._timeout_error or self._make_timeout_error()
                        raise
                self._raise_if_parent_aborted()
                self._refresh_timeout()
                if self._timed_out:
                    raise self._timeout_error or self._make_timeout_error()
                remaining = max(0.0, self._deadline - time.monotonic())
                deadline_waiter = asyncio.create_task(asyncio.sleep(remaining))
                deadline_generation = self._deadline_generation
                reset_waiter = asyncio.create_task(
                    self._wait_for_deadline_change(deadline_generation)
                )
                try:
                    done, _pending = await asyncio.wait(
                        {task, parent_waiter, signal_waiter, deadline_waiter, reset_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for waiter in (deadline_waiter, reset_waiter):
                        if not waiter.done():
                            waiter.cancel()
                    await asyncio.gather(
                        deadline_waiter,
                        reset_waiter,
                        return_exceptions=True,
                    )
                if task in done:
                    return task.result()
                if parent_waiter in done:
                    self._raise_if_parent_aborted()
                if signal_waiter in done and self.signal.aborted:
                    raise self.signal.reason or AbortError()
        except BaseException:
            if not task.done():
                task.cancel()
            if not task.done():
                await asyncio.wait({task}, timeout=0.05)
            if task.done():
                with _contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
            else:
                task.add_done_callback(_consume_task_result)
            raise
        finally:
            for waiter in (parent_waiter, signal_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(
                parent_waiter,
                signal_waiter,
                return_exceptions=True,
            )

    def dispose(self) -> None:
        self._disposed = True
        self._deadline_changed.set()

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def _raise_if_parent_aborted(self) -> None:
        if not _signal_aborted(self._parent_signal):
            return
        reason = _signal_reason(self._parent_signal)
        self.signal.abort(reason)
        raise reason

    def _refresh_timeout(self) -> None:
        if self._disposed or self._timed_out:
            return
        if time.monotonic() < self._deadline:
            return
        self._timed_out = True
        self._timeout_error = self._make_timeout_error()
        self.signal.abort(self._timeout_error)

    def _make_timeout_error(self) -> BaseException:
        error = self._build_timeout_error()
        return error if isinstance(error, BaseException) else RuntimeError(str(error))

    async def _wait_for_deadline_change(self, generation: int) -> None:
        if self._deadline_generation != generation:
            return
        self._deadline_changed.clear()
        if self._deadline_generation != generation:
            return
        await self._deadline_changed.wait()


def create_provider_request_watchdog(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderRequestWatchdog:
    args = {**(args or {}), **kwargs}
    return ProviderRequestWatchdog(
        parentSignal=args.get("parentSignal"),
        initialTimeoutMs=args["initialTimeoutMs"],
        buildTimeoutError=args["buildTimeoutError"],
    )


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    with _contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


__all__ = ["AbortError", "AbortSignal", "ProviderRequestWatchdog", "create_provider_request_watchdog"]
