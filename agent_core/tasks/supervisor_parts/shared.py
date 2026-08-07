from __future__ import annotations

import asyncio
import subprocess
import threading
from typing import Any


_PROCESS_REGISTRY: dict[str, subprocess.Popen[Any]] = {}
_THREAD_WATCHERS: set[threading.Thread] = set()
_TASK_WATCHERS: set[asyncio.Task[Any]] = set()

# Shared summary for a worker whose process died without recording a result.
# Both the projection-time sweep (live_work) and the daemon-restart
# reconciliation (reconciliation) stamp this exact sentence so downstream
# consumers see one dead-worker narrative regardless of which sweep won.
_DEAD_WORKER_SUMMARY = (
    "Worker process is no longer running; reconciled during projection. "
    "Final completion details were unavailable."
)
