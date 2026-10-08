"""The runtime a host process starts: one per process and runtime home.

``bootstrap_runtime`` builds everything for one session, including a
task-supervisor daemon found or spawned under a fresh id, so every open
session used to bring its own daemon. ``start_runtime`` separates the two
scopes: the runtime owns the home's one daemon and its watchdog for as long as
the host process runs, and sessions (opened through it, in later work) attach
to that daemon rather than starting their own.

A runtime is *started* and *stopped*; it is created new in the process and
nothing of it exists beforehand. The home itself is guarded by an owner lease
(``owner_lease``) so a second host process is refused instead of silently
competing for supervision. Without a daemon (``daemon_enabled=False``) the
runtime takes no lease and can run no sub-agents, as before.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import uuid
from typing import Any

from .._utils import is_process_alive, maybe_await, now_iso
from ..daemon.lifecycle import ensure_task_supervisor_daemon, stop_task_supervisor_daemon
from ..storage import configure_runtime_home, get_state_root, load_daemon_state
from ._daemon_watch import check_daemon_once
from ._ticker import run_scoped_async, start_ticker
from .agent_bindings import is_task_supervisor_gateway
from .owner_lease import RuntimeOwnershipError, acquire_owner_lease, release_owner_lease
from .paths import get_active_runtime_paths, run_with_runtime_paths
from .session_manager import SessionManager
from ..scheduler.timer import Scheduler

DEFAULT_STOP_BOUND_SECONDS = 10.0
_GATEWAY_METHODS = (
    "startBackgroundCommand", "startMainSessionTask", "startSubagentTask", "startHealthCheck", "stopTask",
    "retryTask", "read_task_output", "sendTaskMessage", "readTaskEvents", "readTaskQuestions",
    "answerTaskQuestion", "skipTaskQuestion", "stopSession",
)

_ACTIVE: Runtime | None = None
_ACTIVE_LOCK = threading.Lock()


class Runtime:
    """A started runtime. Build it with :func:`start_runtime`."""

    def __init__(
        self,
        *,
        home: str,
        cwd: str,
        instance_id: str,
        daemon_enabled: bool,
        session_options: Any,
        listener: Any,
        monitor_continuation: bool = True,
    ) -> None:
        self.home = home
        self.cwd = cwd
        self.instance_id = instance_id
        self.daemon_enabled = daemon_enabled
        self.session_options = session_options
        self.listener = listener
        # Off: no new continuation work is claimed, and existing continuation
        # deliveries wait, visibly; they are never turned into notifications.
        self.monitor_continuation_enabled = bool(monitor_continuation)
        self.started_at = now_iso()
        self.pid = os.getpid()
        self.paths = None
        self.daemon: dict[str, Any] | None = None
        self.lease: dict[str, Any] | None = None
        self._daemon_lock = threading.Lock()
        self._daemon_state: dict[str, Any] | None = None
        self._recoveries: list[dict[str, Any]] = []
        self._watchdog_stop: Any = None
        self._stopped = False
        self._stop_summary: dict[str, Any] = {}
        self._stop_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe: Any = None
        self._last_cursor: int | None = None
        self.sessions = SessionManager(self)
        self.scheduler = Scheduler(self)

    # -- lifecycle -----------------------------------------------------

    async def _start(self, startup_timeout_seconds: float | None) -> None:
        self._loop = asyncio.get_running_loop()
        self.sessions.idle.start()  # one idle timer for every session, daemon or not
        self.sessions.monitors.start()  # one monitor intake for every session, daemon or not
        self.scheduler.start()  # one timer, only where something can be published
        if not self.daemon_enabled:
            return
        self.lease = await acquire_owner_lease(self.home, self.instance_id)
        try:
            request: dict[str, Any] = {"instanceId": self.instance_id, "cwd": self.cwd, "ownerPid": self.pid}
            if startup_timeout_seconds is not None:
                request["startupTimeoutSeconds"] = startup_timeout_seconds
            self.daemon = await ensure_task_supervisor_daemon(request)
            self._daemon_state = await load_daemon_state(self.instance_id)
            self._subscribe(self.daemon)
            stop = threading.Event()
            start_ticker(
                name=f"runtime-watchdog-{self.instance_id[:12]}",
                stop=stop,
                interval=self._heartbeat_seconds(),
                run_once=self._watchdog_tick,
                halted=stop.is_set,
            )
            self._watchdog_stop = stop.set
        except BaseException:
            await self.scheduler.stop()
            await self.sessions.monitors.stop()
            await self.sessions.idle.stop()
            with contextlib.suppress(Exception):
                await release_owner_lease(self.home, self.instance_id)
            self.lease = None
            raise

    async def stop(self, grace: float = 0.0) -> dict[str, Any]:
        """Stop the runtime, and say what that did. No new session or turn is
        accepted from the first moment; running turns may finish within
        ``grace`` seconds, then are aborted; every session closes
        (``runtime_stopped``); the daemon's workers are stopped through its
        own ladder with records that say ``runtime_stopped`` (never
        resumed, effects of running tools uncertain); the daemon exits,
        verified within a bound; the lease is released. Idempotent and safe
        from a signal-driven shutdown. Stopping is not undoing."""
        async with self._stop_lock:
            if self._stopped:
                return dict(self._stop_summary)
            self._stopped = True
            self.sessions.stopping = True
            summary: dict[str, Any] = {"sessions": None, "workers": None, "daemon": None}
            try:
                if self._watchdog_stop is not None:
                    self._watchdog_stop()
                await self.scheduler.stop()
                await self.sessions.monitors.stop()
                await self.sessions.idle.stop()
                await self.sessions.pending.stop()
                summary["sessions"] = await self.sessions.close_all(reason="runtime_stopped", grace=grace)
                if self.daemon_enabled:
                    summary["workers"] = await self._stop_workers()
                    summary["daemon"] = await self._stop_daemon()
            finally:
                if self.lease is not None:
                    with contextlib.suppress(Exception):
                        await release_owner_lease(self.home, self.instance_id)
                    self.lease = None
                self._stop_summary = summary
                _clear_active(self)
            return dict(summary)

    async def _stop_workers(self) -> dict[str, Any] | None:
        with self._daemon_lock:
            client = self.daemon
        if client is None or not callable(client.get("stopWorkers")):
            return None
        try:
            return await client["stopWorkers"]({"reason": "runtime_stopped"})
        except Exception as error:
            return {"error": str(error)[:256]}

    async def _stop_daemon(self) -> dict[str, Any]:
        self._drop_subscription()
        with self._daemon_lock:
            client, self.daemon = self.daemon, None
        pid = (self._daemon_state or {}).get("pid")
        if client is not None:
            with contextlib.suppress(Exception):
                await client["close"]()
        await stop_task_supervisor_daemon({"instanceId": self.instance_id})
        if isinstance(pid, int) and pid != self.pid:
            deadline = asyncio.get_running_loop().time() + DEFAULT_STOP_BOUND_SECONDS
            while is_process_alive(pid) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            if is_process_alive(pid):
                raise RuntimeError(f"Daemon process {pid} of runtime {self.instance_id} did not exit within {DEFAULT_STOP_BOUND_SECONDS:.0f}s.")
        self._daemon_state = None
        return {"pid": pid, "exited": True}

    # -- the daemon watchdog -------------------------------------------

    def _heartbeat_seconds(self) -> float:
        from ..config._knobs import knob_value

        return int(knob_value("daemon.heartbeat_ms")) / 1000

    def _watchdog_tick(self) -> None:
        """One heartbeat, on the ticker thread. Exactly one watchdog exists per
        runtime, so a lost daemon is replaced once, not once per session."""
        if self._stopped:
            return

        async def tick() -> None:
            outcome = await check_daemon_once(
                {
                    "instanceId": self.instance_id,
                    "cwd": self.cwd,
                    "runtimePaths": self.paths,
                    "emit": self._record_daemon_event,
                    "setDaemon": self._swap_daemon,
                }
            )
            if outcome != "restart_failed":
                self._daemon_state = await load_daemon_state(self.instance_id)

        try:
            run_scoped_async(self.paths, tick)
        except Exception as error:  # the ticker never reports; the next tick tries again
            self._record_daemon_event("daemon.watchdog_error", {"reason": str(error)[:512]})

    async def _swap_daemon(self, client: dict[str, Any]) -> None:
        self._drop_subscription()
        with self._daemon_lock:
            previous, self.daemon = self.daemon, client
        if previous is not None:
            with contextlib.suppress(Exception):
                await previous["close"]()
        # A fresh daemon has a fresh event log: sessions follow it from its start.
        self._last_cursor = None
        self._subscribe(client)

    # -- the runtime's one daemon subscription ---------------------------

    def _subscribe(self, client: dict[str, Any]) -> None:
        """One subscription for every session, routed in-process by session
        id. The daemon announces on its own thread; routing happens on the
        runtime's loop, where sessions live."""
        subscribe = client.get("subscribe")
        if not callable(subscribe):
            return
        kwargs: dict[str, Any] = {}
        if self._last_cursor is not None:
            kwargs["afterCursor"] = self._last_cursor
        self._unsubscribe = subscribe(self._on_daemon_record, None, **kwargs)

    def _drop_subscription(self) -> None:
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if callable(unsubscribe):
            with contextlib.suppress(Exception):
                unsubscribe()

    def _on_daemon_record(self, record: dict[str, Any]) -> None:
        cursor = record.get("cursor")
        if isinstance(cursor, int):
            self._last_cursor = cursor
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self.sessions.route_daemon_event, dict(record))

    def daemon_proxy(self) -> dict[str, Any] | None:
        """The daemon as a session may hold it: every gateway call goes to the
        runtime's current client (so a recovered daemon is picked up), and
        ``close`` does nothing, because the daemon is not the session's."""
        if not self.daemon_enabled:
            return None

        def delegate(name: str) -> Any:
            def call(*args: Any, **kwargs: Any) -> Any:
                with self._daemon_lock:
                    client = self.daemon
                if client is None or not callable(client.get(name)):
                    raise RuntimeError(f"The task supervisor daemon is not connected ({name}).")
                return client[name](*args, **kwargs)

            return call

        template = self.daemon or {}
        proxy = {name: delegate(name) for name, value in template.items() if callable(value) and name not in {"close", "subscribe"}}
        for name in _GATEWAY_METHODS:
            proxy.setdefault(name, delegate(name))
        proxy["close"] = lambda: None
        proxy["subscribe"] = lambda *_args, **_kwargs: (lambda: None)
        proxy["_daemonProxy"] = True
        assert is_task_supervisor_gateway(proxy)
        return proxy

    def _record_daemon_event(self, event: str, data: dict[str, Any]) -> None:
        if event in {"daemon.restarted", "daemon.restart_failed"}:
            self._recoveries.append({"event": event, "at": now_iso(), **{k: v for k, v in data.items() if k != "instance_id"}})
            del self._recoveries[:-20]
        sink = getattr(self.listener, "on_runtime_event", None)
        if callable(sink):
            with contextlib.suppress(Exception):
                sink(event, data)

    # -- what the host can ask -----------------------------------------

    @property
    def stopped(self) -> bool:
        return self._stopped

    async def inspect(self) -> dict[str, Any]:
        """``status()`` plus what lives on disk: every session's children and
        every monitor delivery, with ids and lifecycle reasons only."""
        from .session_inspect import inspect_runtime

        return await inspect_runtime(self)

    def status(self) -> dict[str, Any]:
        """What this runtime owns and how it is doing, from what it last
        observed (the watchdog refreshes the daemon's state every heartbeat)."""
        state = self._daemon_state or {}
        daemon: dict[str, Any] | None = None
        if self.daemon_enabled:
            daemon = {
                "instanceId": self.instance_id,
                "pid": state.get("pid"),
                "epoch": state.get("epoch"),
                "phase": state.get("phase"),
                "socketPath": state.get("socketPath"),
                "ownerPid": self.pid,
                "connected": self.daemon is not None,
                "recoveries": len([r for r in self._recoveries if r["event"] == "daemon.restarted"]),
                "lastRecovery": self._recoveries[-1] if self._recoveries else None,
            }
        return {
            "home": self.home,
            "cwd": self.cwd,
            "instanceId": self.instance_id,
            "pid": self.pid,
            "startedAt": self.started_at,
            "stopped": self._stopped,
            "daemon": daemon,
            "sessions": self.sessions.status(),
            "scheduler": self.scheduler.status(),
        }


async def start_runtime(
    runtime_home: str | None = None,
    *,
    cwd: str | None = None,
    daemon_enabled: bool = True,
    session_options: Any = None,
    listener: Any = None,
    startup_timeout_seconds: float | None = None,
    monitor_continuation: bool = True,
) -> Runtime:
    """Start the runtime for ``runtime_home`` (default: ``$AGENT_CORE_HOME`` or
    the engine's default home) in this process.

    One daemon-owning runtime per process: a second call while one is running
    is refused, whichever home it names; daemon-less runtimes may coexist. One
    host per runtime home: a call from another live process is refused with
    that process's pid. ``session_options`` and
    ``listener`` are kept for the session manager.
    """
    if runtime_home:
        configure_runtime_home(runtime_home)
    home = get_state_root()
    workspace = os.path.abspath(cwd) if cwd else os.getcwd()
    runtime = Runtime(
        home=home,
        cwd=workspace,
        instance_id=str(uuid.uuid4()),
        daemon_enabled=daemon_enabled,
        session_options=session_options,
        listener=listener,
        monitor_continuation=monitor_continuation,
    )
    _reserve_active(runtime)
    try:
        runtime.paths = get_active_runtime_paths()
        await maybe_await(run_with_runtime_paths(runtime.paths, lambda: runtime._start(startup_timeout_seconds)))
    except BaseException:
        _clear_active(runtime)
        raise
    return runtime


def active_runtime() -> Runtime | None:
    """The runtime running in this process, if any."""
    return _ACTIVE


def _reserve_active(runtime: Runtime) -> None:
    """One daemon-owning runtime per process. A daemon-less runtime (a
    companion that only runs turns, such as a memory distiller) owns nothing
    another runtime could compete for and is never the process's one."""
    global _ACTIVE
    if not runtime.daemon_enabled:
        return
    with _ACTIVE_LOCK:
        if _ACTIVE is not None and not _ACTIVE.stopped:
            raise RuntimeOwnershipError(
                runtime.home,
                {"pid": os.getpid(), "instanceId": _ACTIVE.instance_id, "home": _ACTIVE.home},
                f"A runtime is already running in this process for {_ACTIVE.home}; one runtime per host process.",
            )
        _ACTIVE = runtime


def _clear_active(runtime: Runtime) -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is runtime:
            _ACTIVE = None
