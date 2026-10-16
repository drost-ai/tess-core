"""Turns inside an open session: one at a time, in order, each named by an id
and armed with its own abort signal.

A session runs exactly one turn at a time; a second ``run_turn`` waits behind
the first, and different sessions never wait for each other. Every turn is
announced in its ``turn.started`` event with its id and its origin (``user``
for a client's prompt; ``child_message`` and ``monitor`` for the turns the
runtime starts itself; ``scheduled`` for a scheduler's fire), so a cancel can only hit the turn it names: a running
one is aborted through the engine's abort signal, a queued one is dropped
before it starts, a finished or unknown one is a no-op.

ORDER: the user's turns go first, without interrupting whatever is running.
Fairness: when the oldest waiting background turn has waited longer than
``sessions.background_max_wait_ms`` it takes the next slot; that age is never
reset by newer arrivals. A background turn may carry a guard (``only_if``)
that is asked when its slot comes: if the work it was queued for is gone (a
user turn's drain claimed it), the turn is skipped, never started.

JOBS: a monitor delivery is more than a turn (claim, author, checkpoint,
publish), so the runtime may queue a *job* instead of a prompt: it holds the
session's slot like a turn and may run an engine turn inside it through its
:class:`TurnSlot`. A session with unfinished monitor cleanup is quarantined:
nothing runs in it until that work settles.

The queue does the bookkeeping a client used to hand-roll around
``run_agent_prompt``: ``startTurn``, the session's per-turn context (sent as
``ephemeralSystemContext``, never persisted), the turn limits, and
``finishTurn`` or ``failTurn``. Results and exceptions reach the caller of
``run_turn`` unchanged; a turn that raises leaves the session usable.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .._utils import maybe_await, now_iso
from ..agent.loop import run_agent_prompt
from ..agent.prompt_content import normalize_agent_prompt
from ..providers._core import AbortSignal
from .agent_bindings import create_agent_runtime_bindings

TURN_ORIGINS = ("user", "child_message", "monitor", "scheduled", "worker")
# After the grace, an aborted turn gets this long to unwind before the
# session's components are closed under it.
ABORT_BOUND_SECONDS = 5.0


@dataclass
class Turn:
    """One requested turn or job, from queued to finished."""

    id: str
    prompt: Any
    origin: str
    limits: dict[str, int] | None
    provider_override: dict[str, Any] | None
    future: asyncio.Future
    only_if: Callable[[], Awaitable[bool] | bool] | None = None
    job: Callable[["TurnSlot"], Awaitable[Any]] | None = None
    tool_names: list[str] | None = None  # this turn's tools, narrowed; None = the session's
    signal: AbortSignal = field(default_factory=AbortSignal)
    state: str = "queued"  # queued | running | finished
    queued_at: str = field(default_factory=now_iso)
    queued_mono: float = field(default_factory=time.monotonic)
    started_at: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "origin": self.origin,
            "kind": "job" if self.job is not None else "turn",
            "state": self.state,
            "queuedAt": self.queued_at,
            "startedAt": self.started_at,
            "waitedSeconds": round(time.monotonic() - self.queued_mono, 3) if self.state == "queued" else None,
            "cancelling": self.signal.aborted and self.state == "running",
        }


class TurnSlot:
    """What a job may do while it holds the session's slot."""

    def __init__(self, turns: "SessionTurns", turn: Turn) -> None:
        self._turns = turns
        self.turn = turn

    @property
    def live(self) -> Any:
        return self._turns._live

    async def run_turn(self, prompt: Any = "", *, monitor_envelope: dict[str, Any] | None = None, suppress_events: bool = False) -> dict[str, Any]:
        """One engine turn inside this job's slot, with the job's origin, id
        and limits. Suppressed, its provisional events reach nobody outside
        the runtime; only the turn lifecycle is announced."""
        extras: dict[str, Any] = {}
        if monitor_envelope is not None:
            extras["monitorEnvelope"] = monitor_envelope
        live = self._turns._live
        if suppress_events:
            live.suppress_events += 1
        try:
            return await self._turns._execute(self.turn, prompt, extras)
        finally:
            if suppress_events:
                live.suppress_events -= 1


class SessionTurns:
    """The turn queue of one open session; built by the manager."""

    def __init__(self, live: Any) -> None:
        self._live = live
        self._queue: deque[Turn] = deque()
        self._running: Turn | None = None
        self._worker: asyncio.Task[None] | None = None

    # -- what the handle and the manager ask ----------------------------

    def busy(self) -> bool:
        return self._running is not None or bool(self._queue)

    def running(self) -> Turn | None:
        return self._running

    def queued(self) -> list[Turn]:
        return list(self._queue)

    def find(self, turn_id: str) -> Turn | None:
        if self._running is not None and self._running.id == turn_id:
            return self._running
        return next((turn for turn in self._queue if turn.id == turn_id), None)

    async def run(
        self,
        prompt: Any,
        *,
        origin: str = "user",
        limits: dict[str, int] | None = None,
        provider_override: dict[str, Any] | None = None,
        turn_id: str | None = None,
        only_if: Callable[[], Awaitable[bool] | bool] | None = None,
        job: Callable[[TurnSlot], Awaitable[Any]] | None = None,
        tool_names: list[str] | None = None,
    ) -> Any:
        """Queue one turn (or job) and wait for its result. The id is the
        caller's or a fresh one; either way it is the turn's ``logicalTurnId``."""
        if origin not in TURN_ORIGINS:
            raise ValueError(f"unknown turn origin: {origin}")
        if self._live.closed:
            raise RuntimeError(f"Session {self._live.id} is not open.")
        turn_id = turn_id or uuid.uuid4().hex
        if self.find(turn_id) is not None:
            raise ValueError(f"Turn {turn_id} is already queued or running in session {self._live.id}.")
        loop = asyncio.get_running_loop()
        turn = Turn(
            id=turn_id,
            prompt=prompt,
            origin=origin,
            limits=dict(limits) if limits else None,
            provider_override=dict(provider_override) if provider_override else None,
            future=loop.create_future(),
            only_if=only_if,
            job=job,
            tool_names=list(tool_names) if tool_names is not None else None,
        )
        self._queue.append(turn)
        self._live.touch()
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._drain())
        return await turn.future

    def cancel(self, turn_id: str) -> bool:
        """Cancel exactly that turn: abort it if it runs, drop it if it waits.
        Anything else is a no-op."""
        running = self._running
        if running is not None and running.id == turn_id:
            self._abort(running)
            return True
        for turn in self._queue:
            if turn.id == turn_id:
                self._queue.remove(turn)
                self._finish_unstarted(turn, "cancelled")
                return True
        return False

    async def shutdown(self, grace: float = 0.0) -> None:
        """The session is closing for good (runtime stop): queued turns never
        start; a running one may finish within the grace, then it is aborted
        and given a short bound to unwind."""
        while self._queue:
            self._finish_unstarted(self._queue.popleft(), "cancelled")
        worker = self._worker
        if worker is None or worker.done():
            return
        if grace > 0:
            await asyncio.wait({worker}, timeout=grace)
        if not worker.done():
            running = self._running
            if running is not None:
                self._abort(running)
            await asyncio.wait({worker}, timeout=ABORT_BOUND_SECONDS)

    # -- running ---------------------------------------------------------

    def _pick(self) -> Turn:
        """User first; a background turn that has waited past the fairness
        limit takes the slot instead."""
        oldest_background = next((turn for turn in self._queue if turn.origin != "user"), None)
        if oldest_background is not None and time.monotonic() - oldest_background.queued_mono >= _background_max_wait_seconds():
            chosen = oldest_background
        else:
            chosen = next((turn for turn in self._queue if turn.origin == "user"), None) or self._queue[0]
        self._queue.remove(chosen)
        return chosen

    async def _drain(self) -> None:
        while self._queue:
            await self._live.settle_quarantine()  # unfinished monitor cleanup: nothing runs until it settles
            if not self._queue:
                break
            turn = self._pick()
            if turn.only_if is not None:
                try:
                    wanted = await maybe_await(turn.only_if())
                except Exception:
                    wanted = True  # a broken guard never loses work
                if not wanted:
                    self._finish_unstarted(turn, "skipped")
                    self._live.touch()
                    continue
            self._running = turn
            try:
                await self._run_one(turn)
            finally:
                self._running = None
                self._live.touch()  # the idle clock starts when the turn ends

    async def _run_one(self, turn: Turn) -> None:
        turn.state = "running"
        turn.started_at = now_iso()
        try:
            result = await (turn.job(TurnSlot(self, turn)) if turn.job is not None else self._execute(turn, turn.prompt, {}))
        except BaseException as error:
            turn.state = "finished"
            if not turn.future.done():
                turn.future.set_exception(error)
                turn.future.exception()  # retrieved here; the caller may be gone
            if isinstance(error, asyncio.CancelledError):
                raise
            return
        turn.state = "finished"
        if not turn.future.done():
            turn.future.set_result(result)

    async def _execute(self, turn: Turn, prompt: Any, extras: dict[str, Any]) -> dict[str, Any]:
        """One engine turn in this slot: startTurn, the request, finishTurn
        or failTurn. Raises what the engine raises."""
        live = self._live
        controller = live.controller
        _content, prompt_text = normalize_agent_prompt(prompt)
        controller["startTurn"](
            {
                "prompt": prompt_text,
                "model": live.record.get("model") or live.config.get("model"),
                "turnId": turn.id,
                "origin": turn.origin,
            }
        )
        try:
            request: dict[str, Any] = {
                "session": live.record,
                "config": live.config,
                "prompt": prompt,
                "logicalTurnId": turn.id,
                # A fresh signal per turn: the bindings are immutable, so the
                # turn gets its own copy armed with it (narrowed to the turn's
                # tools when the caller named them).
                "runtime": create_agent_runtime_bindings({**live.bindings, "abortSignal": turn.signal, **_narrowed_tools(live, turn.tool_names)}),
                **extras,
                "turnOrigin": turn.origin,
            }
            context = await self._context()
            if context:
                request["ephemeralSystemContext"] = context
            if turn.limits is not None:
                request["turnLimits"] = turn.limits
            override = turn.provider_override if turn.provider_override is not None else live.options.provider_override
            if override is not None:
                request["providerOverride"] = dict(override)
            result = await run_agent_prompt(request)
        except BaseException as error:
            controller["failTurn"](error)
            raise
        kind = "cancelled" if result.get("stopReason") == "cancelled" else "completed"
        controller["finishTurn"](
            {
                "kind": kind,
                "stopReason": result.get("stopReason"),
                # A monitor turn's text is published durably, never streamed.
                "message": None if turn.origin == "monitor" else result.get("text"),
                "model": result.get("model"),
                # The engine emits turn.cancelled itself, with the partial text.
                "announced": kind == "cancelled",
            }
        )
        return result

    async def _context(self) -> str | None:
        provider = self._live.options.context_provider
        if not callable(provider):
            return None
        value = await maybe_await(provider(self._live.id))
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("context_provider must return a string or None")
        return value

    def _abort(self, turn: Turn) -> None:
        if turn.signal.aborted:
            return
        turn.signal.abort()
        self._live.controller["setTurnCancelling"]()

    def _finish_unstarted(self, turn: Turn, stop_reason: str) -> None:
        """A turn that never started reports itself in the same shape as the
        engine's own results, without a turn.started."""
        turn.state = "finished"
        if not turn.future.done():
            live = self._live
            turn.future.set_result(
                {
                    "text": "",
                    "toolCount": 0,
                    "usage": {},
                    "model": live.record.get("model") or live.config.get("model"),
                    "estimatedCostUsd": 0.0,
                    "stopReason": stop_reason,
                }
            )


def _narrowed_tools(live: Any, names: list[str] | None) -> dict[str, Any]:
    if names is None:
        return {}
    wanted = set(names)
    refresh = live.bindings["refreshMcpToolRegistry"]

    def keep(tools: Any) -> Any:
        return [tool for tool in tools if (getattr(tool, "name", None) or tool.get("name")) in wanted] if isinstance(tools, (list, tuple)) else tools

    async def refresh_narrowed() -> Any:
        return keep(await maybe_await(refresh()))

    return {"tools": keep(list(live.bindings["tools"])), "refreshMcpToolRegistry": refresh_narrowed}


def _background_max_wait_seconds() -> float:
    from ..config._knobs import knob_value

    return int(knob_value("sessions.background_max_wait_ms")) / 1000
