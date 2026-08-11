"""Direction-2 dead-man switch: the runtime watches its containment daemon.

A heartbeat ticker checks the daemon's state-file pid AND that its socket
still accepts connections — a bare pid check reads a wedged listener or a
recycled pid as healthy forever. When the daemon is
gone (crash, SIGKILL), the runtime replaces it with exactly ONE fresh
instance — ensureTaskSupervisorDaemon's per-instance lock guarantees
single-spawn and kills a wedged predecessor with identity verification.
The fresh daemon never re-adopts workers and never respawns subagents;
orphan handling is the boot reconciliation sweep's job. The parent is
notified through daemon.lost / daemon.restarted / daemon.restart_failed
events.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from .._utils import is_process_alive as _is_process_alive, maybe_await
from ._ticker import run_scoped_async, start_ticker


async def _daemon_socket_accepting(instance_id: str, state: dict[str, Any] | None) -> bool:
    """A live pid is not a live daemon: a recycled pid or an exited daemon
    behind a stale state file reads alive forever under kill(pid, 0), so
    health additionally requires the instance's socket to accept. One short
    retry absorbs a transiently busy listener; a probe that raises (unsafe
    socket path) counts as inactive so the restart path surfaces the error."""
    from ..daemon import isDaemonSocketListenerActive
    from ..storage import daemon_socket_path

    socket_path = (state or {}).get("socketPath") or daemon_socket_path(instance_id)
    for attempt in range(2):
        try:
            if await isDaemonSocketListenerActive(socket_path):
                return True
        except Exception:
            return False
        if attempt == 0:
            await asyncio.sleep(0.25)
    return False


async def check_daemon_once(context: dict[str, Any]) -> str:
    """One heartbeat: returns "healthy", "restarted", or "restart_failed"."""
    from ..storage import load_daemon_state

    instance_id = context["instanceId"]
    # A SIGKILLed daemon stays a zombie child of this runtime until reaped;
    # poll the spawn handle first so pid liveness below is truthful.
    from ..daemon.lifecycle import spawned_daemon_process

    spawned = spawned_daemon_process(instance_id)
    if spawned is not None:
        spawned.poll()
    state = await load_daemon_state(instance_id)
    pid = (state or {}).get("pid")
    pid_alive = (
        isinstance(pid, int)
        and _is_process_alive(pid)
        and not (spawned is not None and spawned.pid == pid and spawned.returncode is not None)
    )
    if pid_alive and await _daemon_socket_accepting(instance_id, state):
        return "healthy"
    emit = context.get("emit") or (lambda _event, _data: None)
    emit(
        "daemon.lost",
        {
            "instance_id": instance_id,
            "last_pid": pid,
            "last_epoch": (state or {}).get("epoch"),
            "reason": "socket_inactive" if pid_alive else "process_dead",
        },
    )
    from ..daemon import ensureTaskSupervisorDaemon

    try:
        client = await ensureTaskSupervisorDaemon({"instanceId": instance_id, "cwd": context.get("cwd")})
    except Exception as error:
        emit("daemon.restart_failed", {"instance_id": instance_id, "reason": str(error)[:2_048]})
        return "restart_failed"
    set_daemon = context.get("setDaemon")
    if callable(set_daemon):
        await maybe_await(set_daemon(client))
    fresh_state = await load_daemon_state(instance_id)
    emit(
        "daemon.restarted",
        {
            "instance_id": instance_id,
            "pid": (fresh_state or {}).get("pid"),
            "epoch": (fresh_state or {}).get("epoch"),
        },
    )
    return "restarted"


def start_daemon_watchdog(context: dict[str, Any]) -> Any:
    """Start the heartbeat ticker; returns a stop() callable."""
    # Call-time import: config wiring must not load at module import.
    from ..config._knobs import knob_value

    stop = threading.Event()
    runtime_paths = context.get("runtimePaths")

    def tick() -> None:
        try:
            run_scoped_async(runtime_paths, lambda: check_daemon_once(context))
        except Exception:
            pass

    start_ticker(
        name=f"daemon-watchdog-{context['instanceId'][:12]}",
        stop=stop,
        interval=int(knob_value("daemon.heartbeat_ms")) / 1000,
        run_once=tick,
        halted=stop.is_set,
    )
    return stop.set
