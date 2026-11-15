"""One total deadline for a monitor delivery, if the configuration sets one.

Claim, authoring, checkpoint and publication of one delivery share it, with a
configured reserve at its end for failure bookkeeping and cancellation
cleanup. Nothing configured means no deadline: the operation runs until it
finishes, and cleanup is cancel-and-quarantine.

Asyncio deadlines cannot preempt code that blocks the event-loop thread
synchronously, and a coroutine that swallows its cancellation keeps running:
such work is never declared terminated. It is quarantined on the session,
which stays unavailable until the work settles.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

DEADLINE_ERROR = "monitor.lifecycle_deadline"


class MonitorLifecycle:
    def __init__(self, seconds: float | None, *, cleanup_seconds: float = 0.0) -> None:
        self.loop = asyncio.get_running_loop()
        self.deadline = None if seconds is None else self.loop.time() + max(0.0, seconds)
        self.cleanup_seconds = 0.0 if seconds is None else min(max(0.0, cleanup_seconds), max(0.0, seconds) / 3)
        self.work_deadline = None if self.deadline is None else self.deadline - self.cleanup_seconds
        self.active = True
        self.tasks: set[asyncio.Task[Any]] = set()
        self.pending: set[asyncio.Task[Any]] = set()

    def check(self) -> None:
        """Raise once the lifecycle is over: a late step never runs."""
        if not self.active or (self.work_deadline is not None and self.loop.time() >= self.work_deadline):
            raise TimeoutError(DEADLINE_ERROR)

    def start(self, coroutine: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)

        def consume_result(done: asyncio.Task[Any]) -> None:
            if not done.cancelled():
                done.exception()

        task.add_done_callback(consume_result)
        return task

    async def run(self, coroutine: Awaitable[Any]) -> Any:
        task = self.start(coroutine)
        if self.work_deadline is None:
            return await task
        done, _ = await asyncio.wait({task}, timeout=max(0.0, self.work_deadline - self.loop.time()))
        if not done:
            raise TimeoutError(DEADLINE_ERROR)
        return task.result()

    async def finish(self, bookkeeping: Callable[[], Awaitable[None]] | None = None) -> None:
        """Invalidate local publication before cancelling, including callbacks
        resumed by cancellation-resistant storage. Never gather or wait_for:
        whatever does not finish inside the reserve is left pending."""
        self.active = False
        until = None if self.deadline is None else min(self.deadline, self.loop.time() + self.cleanup_seconds)
        for task in self.tasks:
            if not task.done():
                task.cancel()
        recovery: asyncio.Task[Any] | None = None
        if bookkeeping is not None and (until is None or self.loop.time() < until):
            async def recover() -> None:
                try:
                    await bookkeeping()
                except Exception:
                    logging.getLogger(__name__).exception("Monitor failure bookkeeping did not finish successfully")

            recovery = self.start(recover())
        try:
            if until is None:
                # No reserve configured: the bookkeeping is ours and is awaited
                # in full; cancelled work gets one loop turn to unwind, the rest
                # is quarantined.
                if recovery is not None:
                    await recovery
                await asyncio.sleep(0)
            elif self.tasks:
                await asyncio.wait(self.tasks, timeout=max(0.0, until - self.loop.time()))
        finally:
            self.pending = {task for task in self.tasks if not task.done()}
            for task in self.pending:
                task.cancel()
