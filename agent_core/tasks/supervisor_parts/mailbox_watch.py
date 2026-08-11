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
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable

from ..._utils import maybe_await
from ...storage import list_task_records, read_task_meta
from ..communication import read_task_events, read_task_mailbox_messages, task_events_path, task_mailbox_path

_WATCHED_TASK_STATUSES = {"backgrounded", "running", "awaiting_input", "reconciling", "degraded"}
_TERMINAL_TASK_STATUSES = {"completed", "failed", "stopped", "terminated", "cancelled", "truncated"}
# Re-announce a still-queued message every N sweeps: level-triggered without
# flooding the daemon event log while a parent is legitimately away.
_REANNOUNCE_SWEEPS = 4


def create_mailbox_watch_loop(
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> Callable[[], Any]:
    last_announced_sweep: dict[str, int] = {}
    sweep_counter = 0
    trace_cursors: dict[str, int] = {}
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
        for event in events[start:]:
            if event.get("type") != "trace":
                continue
            await maybe_await(
                emit(
                    "subagent.trace",
                    {
                        "task_id": task_id,
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
            parent_session_id = message.get("parentSessionId")
            await maybe_await(
                emit(
                    "subagent.message",
                    {
                        "task_id": task_id,
                        "message_id": message.get("messageId"),
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
        return emitted

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
                emitted += await forward_trace_events(task_id)
                emitted += await announce_queued_messages(task_id, terminal=False)
            elif status in _TERMINAL_TASK_STATUSES and task_id not in drained_terminal_tasks:
                emitted += await forward_trace_events(task_id)
                emitted += await announce_queued_messages(task_id, terminal=True)
        return emitted

    async def watch_loop() -> None:
        # Call-time import: the config package imports tasks at module load.
        from ...config._knobs import knobValue

        while True:
            await asyncio.sleep(int(knobValue("daemon.mailbox_watch_ms")) / 1000)
            try:
                await sweep_once()
            except Exception as exc:
                await maybe_await(
                    emit("supervisor.reconcile_failed", {"reason": str(exc), "family": "mailbox_watch"}, None)
                )

    watch_loop.sweep_once = sweep_once  # type: ignore[attr-defined]
    return watch_loop
