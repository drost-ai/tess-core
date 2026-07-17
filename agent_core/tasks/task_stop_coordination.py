from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable


_TEST_HOOK: Callable[[str, str], Any] | None = None


def setTaskStopCoordinationTestHookForTests(
    hook: Callable[[str, str], Any] | None,
) -> None:
    global _TEST_HOOK
    _TEST_HOOK = hook


class TaskStopCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Event] = {}
        self._stop_locks: dict[str, threading.Lock] = {}

    async def acquireStop(self, taskId: str) -> Callable[[], None]:
        """Serialize stop preparation through durable commit for one task.

        The supervisor is sometimes constructed and called through distinct
        ``asyncio.run`` loops, so these locks must not be bound to an event
        loop.  Nonblocking acquisition with cooperative polling preserves
        that property without consuming a worker thread per queued caller.
        """
        with self._lock:
            stop_lock = self._stop_locks.setdefault(taskId, threading.Lock())
        while not stop_lock.acquire(blocking=False):
            await asyncio.sleep(0.001)
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            stop_lock.release()

        return release

    def holdFinalization(self, taskId: str) -> Callable[[], None]:
        with self._lock:
            if taskId in self._gates:
                raise RuntimeError(f"Task stop coordination is already active: {taskId}")
            gate = threading.Event()
            self._gates[taskId] = gate
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            with self._lock:
                if self._gates.get(taskId) is gate:
                    self._gates.pop(taskId, None)
            gate.set()

        return release

    def waitForStopCommit(self, taskId: str) -> None:
        with self._lock:
            gate = self._gates.get(taskId)
        if not gate:
            return
        self._run_sync_hook("finalizer_waiting", taskId)
        gate.wait()

    async def afterSignal(self, taskId: str) -> None:
        if not _TEST_HOOK:
            return
        result = _TEST_HOOK("after_signal_before_commit", taskId)
        if hasattr(result, "__await__"):
            await result

    @staticmethod
    def _run_sync_hook(stage: str, taskId: str) -> None:
        if not _TEST_HOOK:
            return
        result = _TEST_HOOK(stage, taskId)
        if hasattr(result, "__await__"):
            asyncio.run(result)


def createTaskStopCoordinator() -> TaskStopCoordinator:
    return TaskStopCoordinator()


__setTaskStopCoordinationTestHookForTests = setTaskStopCoordinationTestHookForTests
