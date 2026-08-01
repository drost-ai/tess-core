from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar


_Result = TypeVar("_Result")

# Persistence is intentionally isolated from the process-wide default
# executor. A slow filesystem can queue storage work, but it cannot create an
# unbounded set of storage workers or starve unrelated to_thread callers.
STORAGE_IO_MAX_WORKERS = max(1, min(4, os.cpu_count() or 1))
_STORAGE_EXECUTOR = ThreadPoolExecutor(
    max_workers=STORAGE_IO_MAX_WORKERS,
    thread_name_prefix="agent-core-storage",
)
_STATE_LOCK = threading.Lock()
_ACTIVE_OPERATIONS = 0
_PEAK_ACTIVE_OPERATIONS = 0


def _run_in_context(context: contextvars.Context, call: Callable[[], _Result]) -> _Result:
    global _ACTIVE_OPERATIONS, _PEAK_ACTIVE_OPERATIONS
    with _STATE_LOCK:
        _ACTIVE_OPERATIONS += 1
        _PEAK_ACTIVE_OPERATIONS = max(_PEAK_ACTIVE_OPERATIONS, _ACTIVE_OPERATIONS)
    try:
        return context.run(call)
    finally:
        with _STATE_LOCK:
            _ACTIVE_OPERATIONS -= 1


async def runStorageIo(
    operation: Callable[..., _Result],
    /,
    *args: Any,
    cancelCleanup: Callable[[_Result], Any] | None = None,
    **kwargs: Any,
) -> _Result:
    """Run blocking storage work on the bounded, context-aware executor.

    Cancellation is delivered after an already-submitted filesystem operation
    settles. That prevents a task cancellation from abandoning an atomic
    replace or append half-way through its worker lifecycle while retaining
    normal asyncio cancellation at the call boundary.
    """

    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = partial(operation, *args, **kwargs)
    future = loop.run_in_executor(
        _STORAGE_EXECUTOR,
        _run_in_context,
        context,
        call,
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        result = await asyncio.shield(future)
        if cancelCleanup is not None:
            await runStorageIo(cancelCleanup, result)
        raise


def storageExecutorSnapshot() -> dict[str, int]:
    with _STATE_LOCK:
        return {
            "maxWorkers": STORAGE_IO_MAX_WORKERS,
            "activeOperations": _ACTIVE_OPERATIONS,
            "peakActiveOperations": _PEAK_ACTIVE_OPERATIONS,
        }


def resetStorageExecutorPeakForTesting() -> None:
    global _PEAK_ACTIVE_OPERATIONS
    with _STATE_LOCK:
        _PEAK_ACTIVE_OPERATIONS = _ACTIVE_OPERATIONS
