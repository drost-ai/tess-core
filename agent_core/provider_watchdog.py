from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


class AbortError(RuntimeError):
    def __init__(self, message: str = "Request cancelled"):
        super().__init__(message)
        self.name = "AbortError"


class AbortSignal:
    def __init__(self) -> None:
        self.aborted = False
        self.reason: BaseException | None = None

    def abort(self, reason: BaseException | None = None) -> None:
        if self.aborted:
            return
        self.aborted = True
        self.reason = reason or AbortError()


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

    async def race(self, awaitable: Awaitable[Any]) -> Any:
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                self._raise_if_parent_aborted()
                self._refresh_timeout()
                if self._timed_out:
                    raise self._timeout_error or self._make_timeout_error()
                if task.done():
                    try:
                        return task.result()
                    except BaseException as exc:
                        self._refresh_timeout()
                        if self._timed_out:
                            raise self._timeout_error or self._make_timeout_error()
                        raise exc
                remaining = max(0.0, self._deadline - time.monotonic())
                await asyncio.wait(
                    {task},
                    timeout=min(0.05, remaining or 0.001),
                )
        except BaseException:
            if not task.done():
                task.cancel()
            if not task.done():
                await asyncio.wait({task}, timeout=0.05)
            if task.done():
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                task.add_done_callback(_consume_task_result)
            raise

    def dispose(self) -> None:
        self._disposed = True

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


def createProviderRequestWatchdog(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderRequestWatchdog:
    args = {**(args or {}), **kwargs}
    return ProviderRequestWatchdog(
        parentSignal=args.get("parentSignal"),
        initialTimeoutMs=args["initialTimeoutMs"],
        buildTimeoutError=args["buildTimeoutError"],
    )


def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))


def _signal_reason(signal: Any) -> BaseException:
    reason = signal.get("reason") if isinstance(signal, dict) else getattr(signal, "reason", None)
    return reason if isinstance(reason, BaseException) else AbortError()


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


__all__ = ["AbortError", "AbortSignal", "ProviderRequestWatchdog", "createProviderRequestWatchdog"]
