from __future__ import annotations

import asyncio
import subprocess
import threading
from typing import Any


class SupervisorRegistries:
    """Per-supervisor-instance process and watcher tracking.

    One instance per ``create_task_supervisor`` call: two supervisors in the
    same process (tests, embedders, watchdog restarts) must never observe
    each other's workers, and ``close()`` must only reap its own.
    """

    def __init__(self) -> None:
        self.processes: dict[str, subprocess.Popen[Any]] = {}
        self.threadWatchers: set[threading.Thread] = set()
        self.taskWatchers: set[asyncio.Task[Any]] = set()

# Shared summary for a worker whose process died without recording a result.
# Both the projection-time sweep (live_work) and the daemon-restart
# reconciliation (reconciliation) stamp this exact sentence so downstream
# consumers see one dead-worker narrative regardless of which sweep won.
_DEAD_WORKER_SUMMARY = (
    "Worker process is no longer running; reconciled during projection. "
    "Final completion details were unavailable."
)


def _knob_ms(name: str) -> int:
    """Integer knob read; call-time import because config imports tasks."""
    from ...config._knobs import knobValue

    return int(knobValue(name))
