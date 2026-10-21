"""Sessions opened inside a started runtime.

A session is opened, closed and ended; it already exists on disk (or is
created there) and outlives being loaded. Opening it builds what a turn
needs: its configuration from the host's options, its record, its tools and
MCP servers, its event plumbing and its runtime bindings. The daemon is the
runtime's: a session borrows it through a proxy whose ``close`` does nothing,
and the daemon's events reach the session through the runtime's one
subscription, routed here by session id. Closing a session releases its own
components and nothing else.

The host tells the manager how a session is set up, and whether it is
available at all, through ``session_options(session_id, reason)``: it returns
:class:`SessionOptions` or :class:`Unavailable`. The manager asks for a new
session too, with the id it is about to create, so the host can key options
by id from the start.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Callable

from .._utils import maybe_await, now_iso
from ..config.resolution import resolve_config
from ..session.persistence import persist_session, resolve_session
from ..storage.session_archive import load_session
from ..session.runtime import create_session_runtime_controller
from .agent_bindings import create_agent_runtime_bindings, unavailable_daemon_binding
from .events.factory import create_runtime_event
from .generation import create_runtime_session_binding
from .session_components import prepare_runtime_session_components
from .session_end import end_session, ended_marker
from .session_handle import Session  # noqa: F401  (re-exported)
from .session_idle import IdleSweeper
from .session_monitors import MonitorIntake
from .session_options import (  # noqa: F401  (re-exported: the package imports them from here)
    RuntimeStopped,
    SessionBusy,
    SessionEnded,
    SessionOptions,
    SessionUnavailableError,
    Unavailable,
    UnknownSessionError,
)
from .session_turns import SessionTurns
from .session_wake import PendingWork
from .state.store import create_initial_runtime_state, create_runtime_state_store
from .support import StagedRuntimeSource, default_runtime_component_factories, runtime_path_graph_id

OPEN_REASONS = ("client", "child_message", "monitor", "scheduled")
CLOSE_REASONS = ("client", "idle", "runtime_stopped", "ended")


class _LiveSession:
    """Everything an open session holds in memory. Built by the manager."""

    def __init__(self, manager: SessionManager, record: dict[str, Any], config: dict[str, Any], options: SessionOptions) -> None:
        self.manager = manager
        self.record = record
        self.config = config
        self.options = options
        self.id = str(record["id"])
        runtime = manager.runtime
        self.binding = create_runtime_session_binding(
            self.id, 1, 1, runtimeInstanceId=runtime.instance_id, pathGraphId=runtime_path_graph_id(runtime.paths)
        )
        self.state_store = create_runtime_state_store(create_initial_runtime_state(record))
        self.subscribers: list[Callable[[dict[str, Any]], Any]] = []
        self.components: dict[str, Any] | None = None
        self.controller: dict[str, Any] | None = None
        self.bindings: Any = None
        self.turns = SessionTurns(self)
        self.closed = False
        self.opened_at = now_iso()
        self._last_activity = time.monotonic()
        # While > 0 (a monitor turn), provisional events stay inside the runtime.
        self.suppress_events = 0
        # Monitor cleanup that did not finish in its reserve: the session is
        # quarantined until it settles.
        self.monitor_cleanup: set[Any] = set()

    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        payload = {**(data or {})}
        payload.setdefault("session_id", self.id)
        typed = create_runtime_event({"event": event, "data": payload, "defaultSessionId": self.id})
        self.state_store["dispatch"](typed)
        if self.suppress_events and not event.startswith("turn."):
            return  # nothing of a monitor turn streams before its durable publication
        envelope = {"event": event, "data": payload, "typedEvent": typed}
        for listener in list(self.subscribers):
            listener(envelope)
        self.manager._deliver(self.id, envelope)

    @property
    def monitor_cleanup_pending(self) -> bool:
        self.monitor_cleanup = {task for task in self.monitor_cleanup if not task.done()}
        return bool(self.monitor_cleanup)

    async def settle_quarantine(self) -> None:
        while self.monitor_cleanup_pending:
            await asyncio.wait(set(self.monitor_cleanup))

    async def build(self) -> None:
        runtime = self.manager.runtime
        source = StagedRuntimeSource(self.binding, lambda _binding, event, data: self.publish(event, data))
        self.components = await prepare_runtime_session_components(
            {
                "binding": self.binding,
                "session": self.record,
                "config": self.config,
                "emit": source.emit,
                "factories": default_runtime_component_factories(),
                "bootstrapArgs": {"runtimePaths": runtime.paths, "detectWorkspaceIntegrations": False, "scheduler": runtime.scheduler.running, "hostTools": self.options.host_tools},
                "hasSession": True,
            }
        )
        try:
            for event, data in source.activate():
                self.publish(event, data)
            components = self.components

            def merged_tools() -> list[Any]:
                return list(components["tools"])

            async def refresh_tools() -> list[Any] | None:
                await maybe_await(components["refreshMcpTools"]())
                return merged_tools()

            self.bindings = create_agent_runtime_bindings(
                {
                    "tools": merged_tools(),
                    "executionBudget": self.options.execution_budget,
                    "emitEvent": self.publish,
                    "daemon": self.manager.daemon_binding(),
                    "refreshMcpToolRegistry": refresh_tools,
                    "sessionBinding": self.binding,
                    "questionPolicy": self.manager.question_policy(self.id),
                    "includePartialMessages": True,
                    "streamProviderMessages": bool(self.options.stream_provider_messages),
                    "monitorContinuationSupported": bool(runtime.monitor_continuation_enabled),
                }
            )
            self.controller = create_session_runtime_controller(
                session=self.record, config=self.config, binding=self.binding, emit=self.publish
            )
        except BaseException:
            await maybe_await(self.components["close"]())
            raise

    def turn_in_flight(self) -> bool:
        return bool(self.controller and self.controller["isTurnInFlight"]())

    def busy(self) -> bool:
        return self.turns.busy() or self.turn_in_flight() or self.monitor_cleanup_pending

    def touch(self) -> None:
        """Something happened for this session: the idle clock restarts."""
        self._last_activity = time.monotonic()

    def idle_seconds(self) -> float | None:
        """How long the session has had nothing to do; None while busy."""
        if self.busy():
            return None
        return max(0.0, time.monotonic() - self._last_activity)

    def state(self) -> str:
        if self.turns.running() is not None or self.turn_in_flight():
            return "running"
        return "queued" if self.turns.queued() else "idle"

    async def close(self, grace: float = 0.0) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.turns.shutdown(grace)
        finally:
            for task in self.monitor_cleanup:
                task.cancel()  # quarantined work is not waited for at close
            if self.components is not None:
                await maybe_await(self.components["close"]())


class SessionManager:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._live: dict[str, _LiveSession] = {}
        self._handles: dict[str, Session] = {}
        self._opening: dict[str, Any] = {}
        self._ended: set[str] = set()
        self.stopping = False  # runtime.stop() began: no new session or turn
        # What caused the most recent open, close, spawn and stop.
        self.recent: dict[str, dict[str, Any] | None] = {"opened": None, "closed": None, "spawned": None, "stopped": None}
        self._seen_tasks: set[str] = set()
        self.idle = IdleSweeper(self)
        self.pending = PendingWork(self)
        self.monitors = MonitorIntake(self)

    # -- open / close --------------------------------------------------

    async def open(self, session_id: str | None = None, *, reason: str = "client") -> Session:
        """Open an existing session by exact id, or a new one. An open session
        is returned as is. Unknown ids are an error, never a new session."""
        if reason not in OPEN_REASONS:
            raise ValueError(f"unknown open reason: {reason}")
        if self.stopping:
            raise RuntimeStopped("The runtime is stopped; it opens no session.")
        if session_id and session_id in self._ended:
            raise SessionEnded(session_id)
        if session_id and session_id in self._live:
            return self._handles[session_id]
        if session_id and session_id in self._opening:
            await self._opening[session_id]
            return self._handles[session_id]
        target = session_id or uuid.uuid4().hex
        waiter = asyncio.get_running_loop().create_future()
        self._opening[target] = waiter
        try:
            live = await self._build(target, existing=session_id is not None, reason=reason)
            self._live[target] = live
            handle = self._handles.get(target) or Session(self, target)
            self._handles[target] = handle
            waiter.set_result(None)
        except BaseException as error:
            waiter.set_exception(error)
            waiter.exception()  # retrieved: nobody else may await it
            raise
        finally:
            self._opening.pop(target, None)
        self._notify("on_session_opened", target, reason)
        return handle

    async def _build(self, session_id: str, *, existing: bool, reason: str) -> _LiveSession:
        record: dict[str, Any] | None = None
        if existing:
            # By exact id, straight from the store: the resume search hides
            # sessions that have no messages yet, and accepts queries. An
            # ended session is refused before the host is even asked; an
            # unknown one only after the host had its say.
            record = await load_session(session_id)
            if record is not None and record.get("id") != session_id:
                record = None
            marker = ended_marker(record)
            if marker is not None:
                self._ended.add(session_id)
                raise SessionEnded(session_id, marker.get("reason"), marker.get("at"))
        options = await self._options_for(session_id, reason)
        if existing and record is None:
            raise UnknownSessionError(session_id)
        runtime = self.runtime
        cwd = os.path.abspath(os.path.expanduser(options.cwd)) if options.cwd else runtime.cwd
        os.makedirs(cwd, exist_ok=True)
        overrides = options.config_overrides(runtime.home)
        if runtime.provider_registry is not None:
            overrides["providerRegistry"] = runtime.provider_registry
            if record is not None and record.get("providerProfileName"):
                # An existing session keeps the profile and model it is bound
                # to (its turns resolve the session's own first); the opener's
                # selection binds new sessions. Resolve that profile's credentials.
                overrides["provider"] = record["providerProfileName"]
                if record.get("model"):
                    overrides["model"] = record["model"]
        config = await resolve_config(overrides, cwd)
        if options.timezone:
            # The owner's IANA zone: the scheduler tools' default.
            config["ownerTimezone"] = options.timezone
        if callable(options.adjust_config):
            adjusted = await maybe_await(options.adjust_config(config))
            if isinstance(adjusted, dict):
                config = adjusted
        launch = {"cwd": cwd, "originalCwd": cwd}
        if record is None:
            record = await resolve_session({"sessionId": session_id}, config, launch)
            # A session a host opened exists from that moment: the daemon must
            # find it for the session's tasks, and the host may record its id.
            await persist_session(record, config, force=True)
        if options.permission_mode:
            # The record keeps the mode it was created with; the opener's
            # options govern a root session. A child keeps its granted mode.
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            if not metadata.get("parentSessionId"):
                record["permissionMode"] = options.permission_mode
        if getattr(runtime, "shared_mcp_servers", ()) and not (record.get("metadata") or {}).get("parentSessionId"):
            from .shared_mcp import prepare_host_shared_mcp

            await prepare_host_shared_mcp(runtime, config, record)
        live = _LiveSession(self, record, config, options)
        await live.build()
        return live

    async def _options_for(self, session_id: str, reason: str) -> SessionOptions:
        provider = self.runtime.session_options
        if provider is None:
            return SessionOptions()
        options = await maybe_await(provider(session_id, reason))
        if isinstance(options, Unavailable):
            raise SessionUnavailableError(session_id, options)
        if options is None:
            return SessionOptions()
        if not isinstance(options, SessionOptions):
            raise TypeError("session_options must return SessionOptions or Unavailable")
        return options

    async def close(self, session_id: str, *, reason: str = "client") -> None:
        """Unload a session. Refused while a turn runs: close never cancels."""
        if reason not in CLOSE_REASONS:
            raise ValueError(f"unknown close reason: {reason}")
        live = self._live.get(session_id)
        if live is None:
            return
        if live.busy():
            raise SessionBusy(f"Session {session_id} has a turn running or queued; cancel it first.")
        del self._live[session_id]
        await self.release(session_id, live, reason)

    def detach_if_idle(self, session_id: str, limit_ms: int) -> _LiveSession | None:
        """Unload the session now if it has been idle at least ``limit_ms``,
        and hand it back for release. Synchronous on purpose: nothing can
        run between the busy check and the unload, so a turn arriving now
        finds the session gone and reopens it instead of running under a
        close."""
        live = self._live.get(session_id)
        if live is None:
            return None
        idle = live.idle_seconds()
        if idle is None or idle * 1000 < limit_ms:
            return None
        del self._live[session_id]
        return live

    async def release(self, session_id: str, live: _LiveSession, reason: str, grace: float = 0.0) -> None:
        """Close an unloaded session's components and tell the host."""
        try:
            await live.close(grace)
        finally:
            self._notify("on_session_closed", session_id, reason)

    async def close_all(self, *, reason: str = "runtime_stopped", grace: float = 0.0) -> dict[str, Any]:
        """Runtime stop: every session closes, busy or not. Running turns get
        the grace to finish, then are aborted; queued turns never start.
        Says what that did, including cleanup that could not be finished."""
        summary = {"closed": 0, "runningTurns": 0, "queuedTurns": 0, "unfinishedCleanup": 0}
        for session_id in list(self._live):
            live = self._live.pop(session_id)
            summary["closed"] += 1
            summary["runningTurns"] += int(live.turns.running() is not None)
            summary["queuedTurns"] += len(live.turns.queued())
            await self.release(session_id, live, reason, grace)
            summary["unfinishedCleanup"] += int(live.monitor_cleanup_pending)
        return summary

    # -- what the runtime and the host can ask ---------------------------

    def get(self, session_id: str) -> Session | None:
        return self._handles.get(session_id) if session_id in self._live else None

    def open_ids(self) -> list[str]:
        return list(self._live)

    def live(self, session_id: str) -> _LiveSession | None:
        return self._live.get(session_id)

    async def end(self, session_id: str, *, reason: str = "deleted") -> dict[str, Any]:
        """The one explicit instruction: durable, stops this session's
        children and descendants only, fails what was still addressed to it,
        closes it, blocks every reopen. Idempotent."""
        outcome = await end_session(self, session_id, reason)
        self._ended.add(session_id)
        return outcome

    def is_ended(self, session_id: str) -> bool:
        """Known ended in this runtime; the durable marker is checked at open."""
        return session_id in self._ended

    def status(self) -> dict[str, Any]:
        return {
            "open": [self._session_status(session_id, live) for session_id, live in self._live.items()],
            "pending": self.pending.status(),
            "monitors": self.monitors.status(),
            "recent": dict(self.recent),
        }

    @staticmethod
    def _session_status(session_id: str, live: _LiveSession) -> dict[str, Any]:
        """One open session and why it is open: a running turn, queued
        work, or idle for this long (and then not for much longer)."""
        running = live.turns.running()
        idle = live.idle_seconds()
        return {
            "id": session_id,
            "cwd": live.config.get("cwd"),
            "model": live.record.get("model"),
            "permissionMode": live.record.get("permissionMode"),
            "openedAt": live.opened_at,
            "state": live.state(),
            "idleSeconds": None if idle is None else round(idle, 3),
            "turnInFlight": live.turn_in_flight(),
            "turn": running.describe() if running else None,
            "queued": len(live.turns.queued()),
            "monitorCleanupPending": live.monitor_cleanup_pending,
        }

    # -- plumbing ------------------------------------------------------

    def daemon_binding(self) -> Any:
        proxy = self.runtime.daemon_proxy()
        if proxy is None:
            return unavailable_daemon_binding(
                "runtime.daemon_disabled", "This runtime runs without a task supervisor daemon; sub-agents are unavailable."
            )
        return {"state": "connected", "client": proxy}

    def question_policy(self, session_id: str) -> dict[str, Any]:
        listener = self.runtime.listener
        ask = getattr(listener, "ask_question", None)
        if not callable(ask):
            async def unanswered(*_args: Any, **_kwargs: Any) -> str:
                return ""

            return {"interactive": False, "askQuestion": unanswered}

        async def ask_question(question: Any = None, options: Any = None, detail: Any = None) -> str:
            return await maybe_await(ask(session_id, question, options, detail))

        return {"interactive": True, "askQuestion": ask_question}

    def route_daemon_event(self, payload: dict[str, Any]) -> None:
        """A daemon event arrived on the runtime's subscription: hand it to
        the open session it belongs to, and let pending work see it whether
        or not that session is open (a child's message wakes a closed
        parent)."""
        event = str(payload.get("event"))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        target = payload.get("sessionId") or data.get("session_id") or data.get("sessionId")
        live = self._live.get(target) if isinstance(target, str) else None
        if live is not None:
            live.publish(event, dict(data))
        self.pending.on_event(event, dict(data))
        task_id = data.get("task_id") or data.get("taskId")
        if event == "task.updated" and isinstance(task_id, str) and task_id not in self._seen_tasks:
            # The daemon's first word about a task is its spawn.
            self._seen_tasks.add(task_id)
            self._note("spawned", event=event, sessionId=target, taskId=task_id, status=data.get("status"))
        elif event in ("task.stopped", "task.failed", "task.completed"):
            if isinstance(task_id, str) and callable(getattr(self.runtime, "observe_shared_mcp_terminal", None)):
                self.runtime.observe_shared_mcp_terminal(task_id)
            self._note("stopped", event=event, sessionId=target, taskId=task_id,
                       reason=data.get("detail_status") or data.get("detailStatus") or data.get("reason") or data.get("stop_reason"))

    def _deliver(self, session_id: str, envelope: dict[str, Any]) -> None:
        sink = getattr(self.runtime.listener, "on_event", None)
        if callable(sink):
            sink(session_id, envelope)

    def _notify(self, method: str, session_id: str, reason: str) -> None:
        self._note("opened" if method == "on_session_opened" else "closed", sessionId=session_id, reason=reason)
        sink = getattr(self.runtime.listener, method, None)
        if callable(sink):
            sink(session_id, reason)

    def _note(self, kind: str, **data: Any) -> None:
        self.recent[kind] = {"at": now_iso(), **data}
