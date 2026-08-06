from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable


class LifecycleClosedError(RuntimeError):
    code = "runtime.lifecycle_closed"

    def __init__(self, operation: str, state: str):
        super().__init__(f"Cannot {operation}: runtime lifecycle is {state}.")
        self.state = state


class LifecycleCloseTimeoutError(RuntimeError):
    code = "runtime.close_timeout"

    def __init__(self, pending: int, timeoutMs: int):
        super().__init__(
            f"Runtime close did not drain {pending} in-flight operation"
            f"{'s' if pending != 1 else ''} within {timeoutMs}ms."
        )
        self.pending = pending


class LifecycleBarrier:
    def __init__(self) -> None:
        self.state = "open"
        self._in_flight: set[asyncio.Future[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def inFlightCount(self) -> int:
        return len(self._in_flight)

    def assertOpen(self, operation: str) -> None:
        if self.state != "open":
            raise LifecycleClosedError(operation, self.state)

    def track(self, work: Awaitable[Any]) -> asyncio.Future[Any]:
        future = asyncio.ensure_future(work)
        self._in_flight.add(future)
        future.add_done_callback(self._in_flight.discard)
        return future

    def run(self, operation: str, work: Callable[[], Any]) -> asyncio.Future[Any]:
        self.assertOpen(operation)

        async def invoke() -> Any:
            result = work()
            return await result if inspect.isawaitable(result) else result

        return self.track(invoke())

    def close(self, args: dict[str, Any] | None = None, **kwargs: Any) -> asyncio.Task[None]:
        if self._close_task is not None:
            return self._close_task
        values = {**(args or {}), **kwargs}
        self.state = "closing"
        self._close_task = asyncio.create_task(self._close_and_drain(values))
        return self._close_task

    async def _close_and_drain(self, args: dict[str, Any]) -> None:
        cleanup_error: BaseException | None = None
        escalation_task: asyncio.Task[Any] | None = None

        async def escalate_after() -> None:
            delay = max(1, int(args["escalationAfterMs"])) / 1000
            await asyncio.sleep(delay)
            callback = args.get("onEscalation")
            if callback is not None:
                result = callback(len(self._in_flight))
                if inspect.isawaitable(result):
                    await result

        if args.get("escalationAfterMs") is not None and args.get("onEscalation") is not None:
            escalation_task = asyncio.create_task(escalate_after())

        async def close_work() -> None:
            nonlocal cleanup_error
            try:
                cleanup = args.get("cleanup")
                if cleanup is not None:
                    result = cleanup()
                    if inspect.isawaitable(result):
                        await result
            except BaseException as exc:
                cleanup_error = exc
            while self._in_flight:
                await asyncio.gather(*tuple(self._in_flight), return_exceptions=True)
            if cleanup_error is not None:
                raise cleanup_error

        work = asyncio.create_task(close_work())
        try:
            fail_after = args.get("failAfterMs")
            if fail_after is None:
                await work
            else:
                timeout_ms = max(1, int(fail_after))
                try:
                    await asyncio.wait_for(asyncio.shield(work), timeout_ms / 1000)
                except asyncio.TimeoutError as exc:
                    raise LifecycleCloseTimeoutError(len(self._in_flight), timeout_ms) from exc
            if escalation_task is not None:
                if not escalation_task.done():
                    escalation_task.cancel()
                await asyncio.gather(escalation_task, return_exceptions=True)
            self.state = "closed"
        finally:
            if escalation_task is not None and not escalation_task.done():
                escalation_task.cancel()


def createLifecycleBarrier() -> LifecycleBarrier:
    return LifecycleBarrier()


class ConstructionCleanup:
    def __init__(self) -> None:
        self.entries: list[tuple[str, Callable[[], Any]]] = []

    def defer(self, label: str, cleanup: Callable[[], Any]) -> None:
        self.entries.append((label, cleanup))


async def constructWithRollback(build: Callable[[ConstructionCleanup], Awaitable[Any]]) -> Any:
    cleanups = ConstructionCleanup()
    try:
        return await build(cleanups)
    except BaseException as error:
        cleanup_errors: list[RuntimeError] = []
        for label, cleanup in reversed(cleanups.entries):
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except BaseException as cleanup_error:
                cleanup_errors.append(
                    RuntimeError(f"Construction cleanup failed for {label}: {cleanup_error}")
                )
        if cleanup_errors:
            setattr(error, "cleanupErrors", cleanup_errors)
        raise


