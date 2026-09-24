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

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .._utils import maybe_await
from ..config.resolution import resolve_config
from ..session.persistence import persist_session, resolve_session
from ..storage.session_archive import load_session
from ..session.runtime import create_session_runtime_controller
from .agent_bindings import create_agent_runtime_bindings, unavailable_daemon_binding
from .events.factory import create_runtime_event
from .generation import create_runtime_session_binding
from .session_components import prepare_runtime_session_components
from .session_turns import SessionTurns
from .state.store import create_initial_runtime_state, create_runtime_state_store
from .support import StagedRuntimeSource, default_runtime_component_factories, runtime_path_graph_id

OPEN_REASONS = ("client", "child_message", "monitor")
CLOSE_REASONS = ("client", "idle", "runtime_stopped")


@dataclass(frozen=True)
class SessionOptions:
    """How one session is set up. Everything is optional; the runtime home
    is the runtime's and the workspace defaults to the runtime's cwd."""

    cwd: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    add_dirs: tuple[str, ...] = ()
    mcp_servers: tuple[dict[str, Any], ...] = ()
    settings: str | None = None
    strict_mcp_config: bool = False
    system_prompt: str | None = None
    context_provider: Callable[[str], Awaitable[str | None] | str | None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def config_overrides(self, runtime_home: str) -> dict[str, Any]:
        import json

        overrides: dict[str, Any] = {"runtimeHome": runtime_home}
        for key, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("effort", self.effort),
            ("permissionMode", self.permission_mode),
            ("settings", self.settings),
            ("systemPrompt", self.system_prompt),
        ):
            if value is not None:
                overrides[key] = value
        if self.add_dirs:
            overrides["addDir"] = [os.path.abspath(os.path.expanduser(item)) for item in self.add_dirs]
        if self.mcp_servers:
            overrides["mcpConfig"] = [json.dumps(server) for server in self.mcp_servers]
        if self.strict_mcp_config:
            overrides["strictMcpConfig"] = True
        overrides.update(self.extra)
        return overrides


@dataclass(frozen=True)
class Unavailable:
    """The host refuses or defers opening this session."""

    reason: str
    retry: bool = False


class SessionUnavailableError(RuntimeError):
    def __init__(self, session_id: str, unavailable: Unavailable) -> None:
        self.session_id = session_id
        self.reason = unavailable.reason
        self.retry = unavailable.retry
        super().__init__(f"Session {session_id} is unavailable: {unavailable.reason}" + (" (retry later)" if unavailable.retry else ""))


class UnknownSessionError(LookupError):
    """No session with that id exists in the runtime home."""


class SessionBusy(RuntimeError):
    """The session has a turn running or queued; close never cancels."""


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

    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        payload = {**(data or {})}
        payload.setdefault("session_id", self.id)
        typed = create_runtime_event({"event": event, "data": payload, "defaultSessionId": self.id})
        self.state_store["dispatch"](typed)
        envelope = {"event": event, "data": payload, "typedEvent": typed}
        for listener in list(self.subscribers):
            listener(envelope)
        self.manager._deliver(self.id, envelope)

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
                "bootstrapArgs": {"runtimePaths": runtime.paths, "detectWorkspaceIntegrations": False},
                "hasSession": True,
            }
        )
        try:
            for event, data in source.activate():
                self.publish(event, data)
            components = self.components

            async def refresh_tools() -> list[Any] | None:
                await maybe_await(components["refreshMcpTools"]())
                return list(components["tools"])

            self.bindings = create_agent_runtime_bindings(
                {
                    "tools": components["tools"],
                    "emitEvent": self.publish,
                    "daemon": self.manager.daemon_binding(),
                    "refreshMcpToolRegistry": refresh_tools,
                    "sessionBinding": self.binding,
                    "questionPolicy": self.manager.question_policy(self.id),
                    "includePartialMessages": True,
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
        return self.turns.busy() or self.turn_in_flight()

    async def close(self, grace: float = 0.0) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.turns.shutdown(grace)
        finally:
            if self.components is not None:
                await maybe_await(self.components["close"]())


class Session:
    """A stable handle: the session's id plus the manager. It stays valid
    while the session is closed and unloaded."""

    def __init__(self, manager: SessionManager, session_id: str) -> None:
        self._manager = manager
        self.id = session_id

    @property
    def is_open(self) -> bool:
        return self.id in self._manager._live

    def _live_session(self) -> _LiveSession:
        live = self._manager._live.get(self.id)
        if live is None:
            raise RuntimeError(f"Session {self.id} is not open.")
        return live

    @property
    def model(self) -> str:
        live = self._live_session()
        return str(live.record.get("model") or live.config.get("model") or "")

    @property
    def permission_mode(self) -> str:
        live = self._live_session()
        return str(live.record.get("permissionMode") or live.config.get("permissionMode") or "")

    @property
    def message_count(self) -> int:
        return len(self._live_session().record.get("messages") or [])

    @property
    def tool_names(self) -> list[str]:
        return [getattr(tool, "name", None) or tool.get("name") for tool in self._live_session().components["tools"]]

    def subscribe(self, listener: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        """Receive this session's event envelopes, in addition to the runtime listener."""
        live = self._live_session()
        live.subscribers.append(listener)
        return lambda: live.subscribers.remove(listener) if listener in live.subscribers else None

    # -- turns -----------------------------------------------------------

    async def run_turn(
        self,
        prompt: Any,
        *,
        limits: dict[str, int] | None = None,
        turn_id: str | None = None,
        provider_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one user turn after whatever is already queued for this
        session, and return the engine's result. A handle whose session was
        unloaded reopens it first. ``turn_id`` lets the caller name the turn
        before its ``turn.started`` is announced."""
        if not self.is_open:
            await self._manager.open(self.id)
        return await self._live_session().turns.run(
            prompt, origin="user", limits=limits, provider_override=provider_override, turn_id=turn_id
        )

    async def cancel_turn(self, turn_id: str) -> bool:
        """Cancel exactly that turn. True if it was running or queued here."""
        live = self._manager._live.get(self.id)
        return bool(live is not None and live.turns.cancel(turn_id))

    @property
    def busy(self) -> bool:
        live = self._manager._live.get(self.id)
        return bool(live is not None and live.busy())

    def turns(self) -> dict[str, Any]:
        """The running turn and the queue, for a host's status."""
        live = self._manager._live.get(self.id)
        if live is None:
            return {"running": None, "queued": []}
        running = live.turns.running()
        return {"running": running.describe() if running else None, "queued": [turn.describe() for turn in live.turns.queued()]}

    async def close(self) -> None:
        await self._manager.close(self.id)


class SessionManager:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._live: dict[str, _LiveSession] = {}
        self._handles: dict[str, Session] = {}
        self._opening: dict[str, Any] = {}

    # -- open / close --------------------------------------------------

    async def open(self, session_id: str | None = None, *, reason: str = "client") -> Session:
        """Open an existing session by exact id, or a new one. An open session
        is returned as is. Unknown ids are an error, never a new session."""
        if reason not in OPEN_REASONS:
            raise ValueError(f"unknown open reason: {reason}")
        if session_id and session_id in self._live:
            return self._handles[session_id]
        if session_id and session_id in self._opening:
            await self._opening[session_id]
            return self._handles[session_id]
        target = session_id or uuid.uuid4().hex
        import asyncio

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
        options = await self._options_for(session_id, reason)
        runtime = self.runtime
        cwd = os.path.abspath(os.path.expanduser(options.cwd)) if options.cwd else runtime.cwd
        os.makedirs(cwd, exist_ok=True)
        config = await resolve_config(options.config_overrides(runtime.home), cwd)
        launch = {"cwd": cwd, "originalCwd": cwd}
        if existing:
            # By exact id, straight from the store: the resume search hides
            # sessions that have no messages yet, and accepts queries.
            record = await load_session(session_id)
            if record is None or record.get("id") != session_id:
                raise UnknownSessionError(session_id)
        else:
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
        try:
            await live.close()
        finally:
            self._notify("on_session_closed", session_id, reason)

    async def close_all(self, *, reason: str = "runtime_stopped", grace: float = 0.0) -> None:
        """Runtime stop: every session closes, busy or not. Running turns get
        the grace to finish, then are aborted; queued turns never start."""
        for session_id in list(self._live):
            live = self._live.pop(session_id)
            try:
                await live.close(grace)
            finally:
                self._notify("on_session_closed", session_id, reason)

    # -- what the runtime and the host can ask ---------------------------

    def get(self, session_id: str) -> Session | None:
        return self._handles.get(session_id) if session_id in self._live else None

    def open_ids(self) -> list[str]:
        return list(self._live)

    def live(self, session_id: str) -> _LiveSession | None:
        return self._live.get(session_id)

    def status(self) -> dict[str, Any]:
        return {"open": [self._session_status(session_id, live) for session_id, live in self._live.items()]}

    @staticmethod
    def _session_status(session_id: str, live: _LiveSession) -> dict[str, Any]:
        running = live.turns.running()
        return {
            "id": session_id,
            "cwd": live.config.get("cwd"),
            "model": live.record.get("model"),
            "permissionMode": live.record.get("permissionMode"),
            "turnInFlight": live.turn_in_flight(),
            "turn": running.describe() if running else None,
            "queued": len(live.turns.queued()),
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
        the open session it belongs to. Events for closed sessions are
        ignored here; discovering pending work for them is the runtime's job."""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        target = payload.get("sessionId") or data.get("session_id") or data.get("sessionId")
        live = self._live.get(target) if isinstance(target, str) else None
        if live is not None:
            live.publish(str(payload.get("event")), dict(data))

    def _deliver(self, session_id: str, envelope: dict[str, Any]) -> None:
        sink = getattr(self.runtime.listener, "on_event", None)
        if callable(sink):
            sink(session_id, envelope)

    def _notify(self, method: str, session_id: str, reason: str) -> None:
        sink = getattr(self.runtime.listener, method, None)
        if callable(sink):
            sink(session_id, reason)
