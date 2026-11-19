"""Closed is the default state of a session.

An open session is a loaded runtime: tools, MCP server processes, event
plumbing. It stays loaded only while it is used. When no turn is running,
nothing is queued for it, and ``sessions.idle_close_ms`` has passed since it
last did anything, the runtime unloads it (``on_session_closed(id, "idle")``)
exactly as a client's close would: the record is on disk, the handle stays
valid, and the next ``run_turn`` reopens the session through the host's
options and continues the transcript, with the credentials it was opened
with (``session_credentials``). Having children is not a reason to stay
open: their messages wait on disk and are discovered by the runtime, not held
by the session.

One sweep for all sessions: a single task on the runtime's loop, where the
sessions live, so a session is never closed under a turn (the busy check and
the unload are one synchronous step, ``detach_if_idle``). Whatever the sweep
is releasing when the runtime stops is finished, not interrupted.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any


def idle_close_ms() -> int:
    from ..config._knobs import knob_value

    return int(knob_value("sessions.idle_close_ms"))


def sweep_interval_seconds(limit_ms: int) -> float:
    """A tenth of the limit, between 50 ms and 30 s: an idle session is
    unloaded within a tenth of its limit past it."""
    return max(0.05, min(30.0, limit_ms / 10000))


class IdleSweeper:
    """The one idle timer of a runtime's sessions."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._task: asyncio.Task[None] | None = None
        self._inflight: asyncio.Future[Any] | None = None
        self._stopped = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.get_running_loop().create_task(self._run(), name="session-idle-sweep")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        inflight = self._inflight
        if inflight is not None:
            with contextlib.suppress(Exception):
                await inflight

    async def sweep_once(self) -> list[str]:
        """Close every session idle past the limit now; the ids closed."""
        manager = self._manager
        limit = idle_close_ms()
        closed: list[str] = []
        for session_id in list(manager._live):
            if self._stopped:
                break
            live = manager.detach_if_idle(session_id, limit)  # atomic: unloaded or left alone
            if live is None:
                continue
            # The release is its own task: stopping the sweeper waits for it
            # instead of interrupting a half-closed session.
            self._inflight = asyncio.ensure_future(manager.release(session_id, live, "idle"))
            try:
                await asyncio.shield(self._inflight)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._report("session.close_error", {"session_id": session_id, "reason": "idle", "error": str(error)[:512]})
                continue
            finally:
                self._inflight = None
            closed.append(session_id)
        return closed

    async def _run(self) -> None:
        while not self._stopped:
            await asyncio.sleep(sweep_interval_seconds(idle_close_ms()))
            if not self._stopped:
                await self.sweep_once()

    def _report(self, event: str, data: dict[str, Any]) -> None:
        sink = getattr(self._manager.runtime.listener, "on_runtime_event", None)
        if callable(sink):
            with contextlib.suppress(Exception):
                sink(event, data)
