from __future__ import annotations

import asyncio
import os
from typing import Any

from ._utils import now_iso
from .storage import appendDaemonEvent, clearDaemonState, loadDaemonState, saveDaemonState
from .task_supervisor import createTaskSupervisor


_SUPERVISOR: dict[str, Any] | None = None


async def runTaskSupervisorDaemon(cwd: str) -> None:
    supervisor = await ensureTaskSupervisorDaemon({"cwd": cwd})
    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        await supervisor["close"]()


async def connectTaskSupervisorDaemonClient() -> dict[str, Any] | None:
    return _SUPERVISOR


async def ensureTaskSupervisorDaemon(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    global _SUPERVISOR
    args = {**(args or {}), **kwargs}
    if _SUPERVISOR is not None:
        return _SUPERVISOR

    cwd = args.get("cwd") or os.getcwd()

    async def on_event(event: dict[str, Any]) -> None:
        await appendDaemonEvent({"createdAt": now_iso(), **event})

    _SUPERVISOR = await createTaskSupervisor({"cwd": cwd, "onEvent": on_event})
    await saveDaemonState({"pid": os.getpid(), "cwd": cwd, "startedAt": now_iso(), "mode": "in_process_python", "status": "running"})
    return _SUPERVISOR


async def stopTaskSupervisorDaemon() -> None:
    global _SUPERVISOR
    if _SUPERVISOR:
        await _SUPERVISOR["close"]()
    _SUPERVISOR = None
    await clearDaemonState()


async def getTaskSupervisorDaemonState() -> dict[str, Any] | None:
    state = await loadDaemonState()
    if state and _SUPERVISOR:
        state = {**state, "supervisor": _SUPERVISOR["getState"]()}
    return state
