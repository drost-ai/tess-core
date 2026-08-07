"""Garbage collection for dead per-instance daemon directories.

Every runtime bootstrap mints a fresh instance id, so dead instance
directories accumulate without a sweep. Live instances are never touched:
a directory is only removed when its state names a dead pid (or has no
state at all) and nothing in it changed for the reap window.
"""
from __future__ import annotations

import contextlib
import os
import re
import time

from .._utils import is_process_alive as _is_process_alive
from ..storage import (
    daemonInstanceDir,
    getDaemonInstancesDir,
    getStateRoot,
    loadDaemonState,
    parseDaemonInstanceId,
    withRecordTransaction,
)

_DEAD_INSTANCE_DIR_REAP_SECONDS = 60 * 60


async def _reap_daemon_tmp_artifacts(instance_id: str) -> None:
    try:
        names = os.listdir(daemonInstanceDir(instance_id))
    except FileNotFoundError:
        return
    cutoff = time.time() - 60 * 60
    for name in names:
        if not re.match(r"^state\.json\..+\.tmp$", name):
            continue
        path = os.path.join(daemonInstanceDir(instance_id), name)
        with contextlib.suppress(OSError):
            if os.path.getmtime(path) < cutoff:
                os.remove(path)


async def _reap_dead_daemon_instance_dirs() -> None:
    try:
        names = os.listdir(getDaemonInstancesDir())
    except FileNotFoundError:
        return
    cutoff = time.time() - _DEAD_INSTANCE_DIR_REAP_SECONDS
    for name in names:
        try:
            candidate = parseDaemonInstanceId(name)
        except ValueError:
            continue
        target = daemonInstanceDir(candidate)
        try:
            if os.path.getmtime(target) >= cutoff:
                continue
        except OSError:
            continue
        with contextlib.suppress(Exception):
            async with withRecordTransaction(
                f"daemon-instance-{candidate}",
                getStateRoot(),
                timeoutMs=250,
            ):
                state = await loadDaemonState(candidate)
                pid = (state or {}).get("pid")
                if isinstance(pid, int) and _is_process_alive(pid):
                    continue
                await _remove_daemon_instance_dir(candidate)


async def _remove_daemon_instance_dir(instance_id: str) -> None:
    root = daemonInstanceDir(instance_id)
    for directory, _dirs, files in os.walk(root, topdown=False):
        for file_name in files:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(directory, file_name))
        with contextlib.suppress(OSError):
            os.rmdir(directory)
