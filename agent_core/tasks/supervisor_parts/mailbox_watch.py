"""Daemon-side mailbox watch: the parent-wake signal.

LEVEL-TRIGGERED: a queued to_parent message is re-announced every
re-announce interval until the parent's drain claims it and its status
leaves "queued". The old edge-triggered contract (announce once, ever)
lost the message forever when the single announcement landed while the
parent's turn was in flight - the wake was dropped and never retried, and
the parent idled on a finished child's result until the user typed.
Terminal tasks are swept too: a child's dying message ([turn failed]) is
written moments before its record goes terminal, and excluding terminal
statuses made that message unannounceable.

IDENTIFIABLE: a trace event carries ``event_index``, the position of its row
in the task's event log, and a message announcement carries its ``message_id``
plus whether this watch has announced it before. A consumer that keeps what
it receives (a host journal) can therefore tell a repeat from new activity.
An index is an aid for dropping duplicates, not proof of delivery.

QUESTIONS TOO: a child blocked on a question to its parent is announced the
same way (``subagent.question``, level-triggered while the question is
pending) for live tasks, so a parent that is not loaded can be woken to
answer it, exactly as for a message. A question of a finished task is moot.

AND A CHILD'S END: a one-shot child that finished, failed or was stopped
leaves its parent a reconciliation entry instead of a mailbox message. While
that entry is unconsumed it is announced the same way
(``subagent.reconciliation``), so a closed parent is woken for its child's
failure as it is for its child's result; the parent's next turn consumes it.

A watch forwards a task's trace rows once, from a cursor it keeps in memory.
The cursor is deliberately not shared on disk: while several daemons can
sweep one runtime home, a shared cursor would let one daemon consume the
traces another daemon's subscriber is waiting for. So that a new watch does
not replay history instead, it never forwards the trace of a task that was
created before the watch started and is already terminal when the watch first
sees it: every row of such a task was written before this watch existed, and
whoever was watching then has forwarded it. A task this watch saw alive, or
one created during its lifetime, is forwarded to its last row.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import Any, Callable

from ..._utils import maybe_await
from ...storage import list_task_records, read_task_meta
from ..communication import (
    read_task_events,
    read_task_mailbox_messages,
    read_task_questions,
    task_events_path,
    task_mailbox_path,
    task_questions_path,
)

_WATCHED_TASK_STATUSES = {"backgrounded", "running", "awaiting_input", "reconciling", "degraded"}
_TERMINAL_TASK_STATUSES = {"completed", "failed", "stopped", "terminated", "cancelled", "truncated"}
# Re-announce a still-queued message every N sweeps: level-triggered without
# flooding the daemon event log while a parent is legitimately away.
_REANNOUNCE_SWEEPS = 4


def _predates(record: dict[str, Any], moment: float) -> bool:
    """True when the task record was created before ``moment``. A record whose
    creation time cannot be read is treated as current, which only costs a
    replay that consumers can drop by index."""
    created = record.get("createdAt")
    if not isinstance(created, str) or not created:
        return False
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() < moment
    except ValueError:
        return False


def create_mailbox_watch_loop(
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> Callable[[], Any]:
    last_announced_sweep: dict[str, int] = {}
    sweep_counter = 0
    trace_cursors: dict[str, int] = {}
    watch_started_at = time.time()
    seen_live: set[str] = set()
    # Tasks that predate this watch and were already terminal when it first saw them.
    trace_history_only: set[str] = set()
    announced_message_keys: set[str] = set()
    parent_ids: dict[str, str | None] = {}
    # A terminal task's child is dead: once its mailbox shows no queued
    # to_parent messages it can never gain one, so skip it forever.
    drained_terminal_tasks: set[str] = set()

    async def _parent_session_id(task_id: str) -> str | None:
        # Trace events must route on the PARENT session: attached parent
        # runtimes subscribe filtered by their own session id.
        if task_id not in parent_ids:
            meta = await read_task_meta(task_id) or {}
            parent = meta.get("parent_session_id") or meta.get("parentSessionId")
            parent_ids[task_id] = parent if isinstance(parent, str) and parent else None
        return parent_ids[task_id]

    async def forward_trace_events(task_id: str) -> int:
        if not os.path.exists(task_events_path(task_id)):
            return 0
        events = await read_task_events(task_id)
        start = trace_cursors.get(task_id, 0)
        if start >= len(events):
            return 0
        parent_session_id = await _parent_session_id(task_id)
        forwarded = 0
        for event_index, event in enumerate(events[start:], start=start):
            if event.get("type") != "trace":
                continue
            await maybe_await(
                emit(
                    "subagent.trace",
                    {
                        "task_id": task_id,
                        "event_index": event_index,
                        "trace_event": event.get("event"),
                        "tool": event.get("tool"),
                        "input_preview": event.get("input_preview"),
                        "output_chars": event.get("output_chars"),
                        "error": event.get("error"),
                        "text_preview": event.get("text_preview"),
                        "parent_session_id": parent_session_id,
                    },
                    parent_session_id,
                )
            )
            forwarded += 1
        trace_cursors[task_id] = len(events)
        return forwarded

    async def announce_queued_messages(task_id: str, *, terminal: bool) -> int:
        if not os.path.exists(task_mailbox_path(task_id)):
            if terminal:
                drained_terminal_tasks.add(task_id)
            return 0
        emitted = 0
        queued_seen = False
        for message in await read_task_mailbox_messages(task_id):
            if message.get("direction") != "to_parent" or message.get("status") != "queued":
                continue
            queued_seen = True
            key = f"{task_id}:{message.get('messageId')}"
            last = last_announced_sweep.get(key)
            if last is not None and sweep_counter - last < _REANNOUNCE_SWEEPS:
                continue
            last_announced_sweep[key] = sweep_counter
            announcement = "repeat" if key in announced_message_keys else "first"
            announced_message_keys.add(key)
            parent_session_id = message.get("parentSessionId")
            await maybe_await(
                emit(
                    "subagent.message",
                    {
                        "task_id": task_id,
                        "message_id": message.get("messageId"),
                        "announcement": announcement,
                        "kind": message.get("kind"),
                        "sender": message.get("sender"),
                        "parent_session_id": parent_session_id,
                        "preview": str(message.get("content") or "")[:200],
                    },
                    parent_session_id if isinstance(parent_session_id, str) else None,
                )
            )
            emitted += 1
        if terminal and not queued_seen:
            drained_terminal_tasks.add(task_id)
        if not queued_seen:
            prefix = f"{task_id}:"
            for key in [key for key in last_announced_sweep if key.startswith(prefix)]:
                last_announced_sweep.pop(key, None)
                announced_message_keys.discard(key)
        return emitted

    async def announce_pending_questions(task_id: str) -> int:
        if not os.path.exists(task_questions_path(task_id)):
            return 0
        emitted = 0
        pending_seen = False
        for question in await read_task_questions(task_id):
            if question.get("status") != "pending":
                continue
            pending_seen = True
            key = f"q:{task_id}:{question.get('questionId')}"
            last = last_announced_sweep.get(key)
            if last is not None and sweep_counter - last < _REANNOUNCE_SWEEPS:
                continue
            last_announced_sweep[key] = sweep_counter
            announcement = "repeat" if key in announced_message_keys else "first"
            announced_message_keys.add(key)
            parent_session_id = await _parent_session_id(task_id)
            await maybe_await(
                emit(
                    "subagent.question",
                    {
                        "task_id": task_id,
                        "question_id": question.get("questionId"),
                        "announcement": announcement,
                        "child_session_id": question.get("childSessionId"),
                        "parent_session_id": parent_session_id,
                        "created_at": question.get("createdAt"),
                        "preview": str(question.get("question") or "")[:200],
                    },
                    parent_session_id,
                )
            )
            emitted += 1
        if not pending_seen:
            prefix = f"q:{task_id}:"
            for key in [key for key in last_announced_sweep if key.startswith(prefix)]:
                last_announced_sweep.pop(key, None)
                announced_message_keys.discard(key)
        return emitted

    async def announce_unconsumed_reconciliation(task_id: str) -> bool:
        """True while the parent still has this task's reconciliation to consume."""
        from ...subagents.reconciliation import list_unconsumed_subagent_reconciliations

        parent_session_id = await _parent_session_id(task_id)
        key = f"r:{task_id}"
        pending = []
        if parent_session_id:
            pending = [record for record in await list_unconsumed_subagent_reconciliations(parent_session_id) if record.get("taskId") == task_id]
        if not pending:
            last_announced_sweep.pop(key, None)
            announced_message_keys.discard(key)
            return False
        last = last_announced_sweep.get(key)
        if last is None or sweep_counter - last >= _REANNOUNCE_SWEEPS:
            last_announced_sweep[key] = sweep_counter
            announcement = "repeat" if key in announced_message_keys else "first"
            announced_message_keys.add(key)
            record = pending[0]
            await maybe_await(
                emit(
                    "subagent.reconciliation",
                    {
                        "task_id": task_id,
                        "record_id": record.get("id"),
                        "announcement": announcement,
                        "outcome": record.get("status") or record.get("outcome"),
                        "parent_session_id": parent_session_id,
                    },
                    parent_session_id,
                )
            )
        return True

    async def sweep_once() -> int:
        nonlocal sweep_counter
        sweep_counter += 1
        emitted = 0
        for record in await list_task_records():
            if record.get("kind") != "subagent":
                continue
            task_id = record.get("id") or record.get("taskId")
            if not isinstance(task_id, str) or not task_id:
                continue
            status = record.get("status")
            if status in _WATCHED_TASK_STATUSES:
                seen_live.add(task_id)
                emitted += await forward_trace_events(task_id)
                emitted += await announce_queued_messages(task_id, terminal=False)
                emitted += await announce_pending_questions(task_id)
            elif status in _TERMINAL_TASK_STATUSES and task_id not in drained_terminal_tasks:
                if task_id not in seen_live and _predates(record, watch_started_at):
                    trace_history_only.add(task_id)
                if task_id not in trace_history_only:
                    # Ours: forward the tail it wrote while dying.
                    emitted += await forward_trace_events(task_id)
                emitted += await announce_queued_messages(task_id, terminal=True)
                if await announce_unconsumed_reconciliation(task_id):
                    drained_terminal_tasks.discard(task_id)  # still something to tell the parent
        return emitted

    async def watch_loop() -> None:
        # Call-time import: the config package imports tasks at module load.
        from ...config._knobs import knob_value

        while True:
            await asyncio.sleep(int(knob_value("daemon.mailbox_watch_ms")) / 1000)
            try:
                await sweep_once()
            except Exception as exc:
                await maybe_await(
                    emit("supervisor.reconcile_failed", {"reason": str(exc), "family": "mailbox_watch"}, None)
                )

    watch_loop.sweep_once = sweep_once  # type: ignore[attr-defined]
    return watch_loop
