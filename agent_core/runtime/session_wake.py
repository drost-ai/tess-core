"""Work that arrives for a session the runtime is not running a turn for.

A child's result or question is written to disk by the child and announced
by the daemon's mailbox watch, level-triggered, on the PARENT session id,
whether that parent is open, idle or closed. This module turns those
announcements into turns:

- a closed parent is reopened through the host (``options_for(id,
  "child_message")``) and a continuity turn is queued for it;
- an open parent gets the continuity turn queued, unless a user turn is
  already waiting (its drain claims the mailbox anyway);
- at most one continuity turn is pending per session, however often the
  daemon repeats the announcement;
- a host that answers ``Unavailable`` leaves the work where it is, on disk:
  the refusal and its reason show in ``status()``, and the work is offered
  again only if ``retry`` says so, with a doubling delay.

A continuity turn is an empty prompt with the origin ``child_message``: the
engine's own boundary drain claims the queued messages into the transcript
and its open-question reminders surface a child's question. Claiming is not
handling: the mailbox's durable claim, the transcript and the recovery
semantics are the engine's, untouched here, and nothing is marked processed
because a reopen began. The turn runs only if something is still queued when
its slot comes (a user turn may have drained it first). Its ceilings in
provider calls, tool calls and time are the configuration's
(``sessions.background_*``): nothing configured, no ceiling; never a number
chosen here.

A child blocked on a question to a parent the host will not open gets an
explicit runtime-authored answer once the child's own reply bound
(``comms.reply_timeout_ms``) has passed, so it never waits forever.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .._utils import now_iso
from ..tasks.communication import (
    answer_task_question,
    read_task_mailbox_messages,
    read_task_questions,
    task_mailbox_path,
    task_questions_path,
)
from .session_options import SessionEnded, SessionUnavailableError, UnknownSessionError

WAKE_EVENTS = {"subagent.message": "child_message", "subagent.question": "child_message"}
MAX_RETRY_SECONDS = 900.0
UNAVAILABLE_ANSWER = "unanswered:parent_unavailable"


def _knob(name: str) -> int | None:
    from ..config._knobs import knob_value

    value = knob_value(name)
    return None if value is None else int(value)


def background_limits() -> dict[str, int] | None:
    """The configured ceilings of a turn the runtime starts itself; None
    when the configuration sets none (then there is no ceiling)."""
    limits = {
        key: value
        for key, value in (
            ("maxProviderCalls", _knob("sessions.background_max_provider_calls")),
            ("maxToolCalls", _knob("sessions.background_max_tool_calls")),
            ("timeoutMs", _knob("sessions.background_timeout_ms")),
        )
        if value is not None
    }
    return limits or None


async def pending_child_work(record: dict[str, Any]) -> bool:
    """Is anything still waiting for this parent: a queued to_parent message
    or a pending question of one of its tasks?"""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for task_id in metadata.get("taskIds") or []:
        if not isinstance(task_id, str) or not task_id:
            continue
        if os.path.exists(task_mailbox_path(task_id)):
            for message in await read_task_mailbox_messages(task_id):
                if message.get("direction") == "to_parent" and message.get("status") == "queued":
                    return True
        if os.path.exists(task_questions_path(task_id)):
            for question in await read_task_questions(task_id):
                if question.get("status") == "pending":
                    return True
    return False


@dataclass
class Pending:
    """Work waiting for one session, from the first announcement until its
    continuity turn (or the job it was submitted with) is done or the host
    refused for good."""

    session_id: str
    origin: str
    key: str = ""
    run: Callable[[Any], Awaitable[None]] | None = None  # None: a continuity turn
    since: str = field(default_factory=now_iso)
    state: str = "reopening"  # reopening | queued | running | waiting | refused
    turn_id: str | None = None
    attempts: int = 0
    reason: str | None = None
    retry: bool = False
    next_retry_mono: float | None = None
    next_retry_at: str | None = None
    last: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None

    def describe(self, live: Any) -> dict[str, Any]:
        state = self.state
        if live is not None and self.turn_id:
            turn = live.turns.find(self.turn_id)
            if turn is not None:
                state = turn.state
        return {
            "key": self.key,
            "sessionId": self.session_id,
            "origin": self.origin,
            "state": state,
            "since": self.since,
            "turnId": self.turn_id,
            "attempts": self.attempts,
            "reason": self.reason,
            "retry": self.retry,
            "nextRetryAt": self.next_retry_at,
            "last": dict(self.last),
        }


class PendingWork:
    """The runtime's answer to announcements for sessions it is not running."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._items: dict[str, Pending] = {}
        self._backstops: set[str] = set()
        self._stopped = False

    # -- announcements ---------------------------------------------------

    def on_event(self, event: str, data: dict[str, Any]) -> None:
        """A daemon event arrived on the runtime's subscription."""
        origin = WAKE_EVENTS.get(event)
        if origin is None or self._stopped:
            return
        session_id = data.get("parent_session_id") or data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        manager = self._manager
        if manager.is_ended(session_id):
            if event == "subagent.question":
                self._backstop_if_overdue(session_id, data, "ended")
            return
        item = self._items.get(session_id)
        if item is not None:
            item.last = {k: data.get(k) for k in ("event", "task_id", "message_id", "question_id") if data.get(k) is not None}
            item.last["event"] = event
            if item.state in ("reopening", "queued", "running"):
                return  # one pending continuity per session, however often the daemon repeats
            if event == "subagent.question":
                self._backstop_if_overdue(session_id, data, item.reason or "unavailable")
            if not self._due(item):
                return
        live = manager.live(session_id)
        if live is not None:
            if any(turn.origin == "user" for turn in live.turns.queued()):
                return  # a waiting user turn drains the mailbox itself
            self._queue(session_id, live, origin, item)
        else:
            self._reopen(session_id, origin, item)

    def submit(self, key: str, session_id: str, origin: str, run: Callable[[Any], Awaitable[None]]) -> bool:
        """Work the runtime's own intake found for a session (a monitor
        delivery): run it in the session's slot once the session is open,
        reopening it through the host if needed. One item per key; a
        refusal's delay is honoured. True if it was taken up now."""
        if self._stopped or self._manager.is_ended(session_id):
            return False
        item = self._items.get(key)
        if item is not None and (item.state in ("reopening", "queued", "running") or not self._due(item)):
            return False
        item = item or Pending(session_id, origin, key=key, run=run)
        self._items[key] = item
        live = self._manager.live(session_id)
        if live is not None:
            self._start(item, live)
        else:
            item.state = "reopening"
            item.task = asyncio.get_running_loop().create_task(self._reopen_task(item))
        return True

    @staticmethod
    def _due(item: Pending) -> bool:
        if item.state == "refused":
            return False  # the host said no, without retry: never offered again
        return item.next_retry_mono is None or time.monotonic() >= item.next_retry_mono

    # -- reopening -------------------------------------------------------

    def _reopen(self, session_id: str, origin: str, item: Pending | None) -> None:
        item = item or Pending(session_id, origin, key=session_id)
        self._items[session_id] = item
        item.state = "reopening"
        item.task = asyncio.get_running_loop().create_task(self._reopen_task(item))

    async def _reopen_task(self, item: Pending) -> None:
        manager = self._manager
        try:
            await manager.open(item.session_id, reason=item.origin)
        except SessionUnavailableError as refused:
            self._refuse(item, refused.reason, refused.retry)
            return
        except SessionEnded:
            self._refuse(item, "ended", False)  # never offered again, the host not asked
            return
        except UnknownSessionError:
            self._refuse(item, "unknown_session", False)
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._refuse(item, f"open_failed: {str(error)[:200]}", True)
            return
        live = manager.live(item.session_id)
        if live is None:
            self._items.pop(item.key or item.session_id, None)
            return
        if item.run is not None:
            self._start(item, live)
        else:
            self._queue(item.session_id, live, item.origin, item)

    def _start(self, item: Pending, live: Any) -> None:
        item.state = "queued"
        item.reason, item.retry, item.next_retry_mono, item.next_retry_at = None, False, None, None
        item.task = asyncio.get_running_loop().create_task(self._run_submitted(item, live))

    async def _run_submitted(self, item: Pending, live: Any) -> None:
        try:
            item.state = "running"
            await item.run(live)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._report("session.background_failed", {"session_id": item.session_id, "origin": item.origin, "key": item.key, "error": str(error)[:512]})
        finally:
            if self._items.get(item.key) is item:
                del self._items[item.key]

    def _refuse(self, item: Pending, reason: str, retry: bool) -> None:
        item.attempts += 1
        item.reason = reason
        item.retry = retry
        if retry:
            item.state = "waiting"
            base_ms = _knob("sessions.pending_retry_ms")
            if base_ms is None:
                # Nothing configured: offered again at the daemon's next announcement.
                item.next_retry_mono, item.next_retry_at = None, None
            else:
                delay = min(MAX_RETRY_SECONDS, base_ms / 1000 * (2 ** (item.attempts - 1)))
                item.next_retry_mono = time.monotonic() + delay
                item.next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
        else:
            item.state = "refused"
            item.next_retry_mono = None
            item.next_retry_at = None
        self._report(
            "session.reopen_refused",
            {"session_id": item.session_id, "origin": item.origin, "reason": reason, "retry": retry, "attempts": item.attempts, "next_retry_at": item.next_retry_at},
        )

    # -- the continuity turn ---------------------------------------------

    def _queue(self, session_id: str, live: Any, origin: str, item: Pending | None) -> None:
        item = item or Pending(session_id, origin, key=session_id)
        self._items[session_id] = item
        item.state = "queued"
        item.turn_id = uuid.uuid4().hex
        item.reason, item.retry, item.next_retry_mono, item.next_retry_at = None, False, None, None
        item.task = asyncio.get_running_loop().create_task(self._continuity(item, live))

    async def _continuity(self, item: Pending, live: Any) -> None:
        record = live.record
        try:
            await live.turns.run(
                "",
                origin=item.origin,
                limits=background_limits(),
                turn_id=item.turn_id,
                only_if=lambda: pending_child_work(record),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._report("session.continuity_failed", {"session_id": item.session_id, "turn_id": item.turn_id, "error": str(error)[:512]})
        finally:
            if self._items.get(item.session_id) is item:
                del self._items[item.session_id]

    # -- a question nobody will answer -----------------------------------

    def _backstop_if_overdue(self, session_id: str, data: dict[str, Any], why: str) -> None:
        """A child's question to a parent the host will not open: answer it
        ourselves once the child's own reply bound has passed."""
        task_id, question_id = data.get("task_id"), data.get("question_id")
        if not isinstance(task_id, str) or not isinstance(question_id, str):
            return
        created = _parse_iso(data.get("created_at"))
        if created is None or (time.time() - created) * 1000 < _knob("comms.reply_timeout_ms"):
            return
        key = f"{task_id}:{question_id}"
        if key in self._backstops:
            return
        self._backstops.add(key)

        async def answer() -> None:
            try:
                answered = await answer_task_question(task_id, question_id, f"{UNAVAILABLE_ANSWER}:{why}")
            except Exception as error:
                self._backstops.discard(key)
                self._report("session.question_backstop_failed", {"session_id": session_id, "task_id": task_id, "question_id": question_id, "error": str(error)[:512]})
                return
            if answered:
                self._report("subagent.question.unanswered", {"session_id": session_id, "task_id": task_id, "question_ids": [question_id], "count": 1, "reason": why})

        asyncio.get_running_loop().create_task(answer())

    # -- what the host can ask -------------------------------------------

    def drop(self, session_id: str, reason: str) -> int:
        """The session was ended: whatever waited for it is cancelled and
        forgotten here (its records on disk carry their own failure)."""
        dropped = 0
        for key, item in list(self._items.items()):
            if item.session_id != session_id:
                continue
            if item.task is not None and not item.task.done():
                item.task.cancel()
            del self._items[key]
            dropped += 1
        if dropped:
            self._report("session.pending_dropped", {"session_id": session_id, "reason": reason, "count": dropped})
        return dropped

    def status(self) -> list[dict[str, Any]]:
        return [item.describe(self._manager.live(item.session_id)) for item in self._items.values()]

    async def stop(self) -> None:
        self._stopped = True
        tasks = [item.task for item in self._items.values() if item.task is not None and not item.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        self._items.clear()

    def _report(self, event: str, data: dict[str, Any]) -> None:
        sink = getattr(self._manager.runtime.listener, "on_runtime_event", None)
        if callable(sink):
            with contextlib.suppress(Exception):
                sink(event, data)


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
