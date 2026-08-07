from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from .paths import runWithRuntimePaths


def run_scoped_async(runtime_paths: Any, work: Callable[[], Any]) -> Any:
    """Run one async work item to completion on a fresh event loop.

    Ticker threads own no long-lived loop: each tick runs inside its own
    ``asyncio.run`` so loop teardown in one tick can never leak into the
    next. ``runtime_paths`` (when set) scopes storage access to the runtime
    that captured them at construction.
    """
    return asyncio.run(
        runWithRuntimePaths(runtime_paths, work) if runtime_paths else work()
    )


def start_oneshot(name: str, run_once: Callable[[], None]) -> threading.Thread:
    """Start a named daemon thread that runs ``run_once`` exactly once."""
    thread = threading.Thread(target=run_once, name=name, daemon=True)
    thread.start()
    return thread


def start_ticker(
    *,
    name: str,
    stop: threading.Event,
    interval: float,
    run_once: Callable[[], None],
    halted: Callable[[], bool] | None = None,
) -> threading.Thread:
    """Start a named daemon thread that runs ``run_once`` every ``interval``.

    The loop waits first, so the initial tick happens one interval after
    start. ``halted`` is consulted after each wait and ends the loop without
    another tick. Error policy belongs to ``run_once``; the loop itself
    never swallows or reports.
    """

    def _loop() -> None:
        while not stop.wait(interval):
            if halted is not None and halted():
                return
            run_once()

    thread = threading.Thread(target=_loop, name=name, daemon=True)
    thread.start()
    return thread
