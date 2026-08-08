"""Daemon-side mailbox watch: the parent-wake signal.

Each sweep announces queued to_parent messages of live subagent tasks as
subagent.message events, exactly once per message. The event rides the
existing daemon-event → client-subscription → runtime-event path, so an
attached parent runtime sees it live and its wake controller can start a
continuity turn when idle.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

from ..._utils import maybe_await
from ...storage import listTaskRecords, readTaskMeta
from ..communication import read_task_events, read_task_mailbox_messages, task_events_path, task_mailbox_path

_WATCHED_TASK_STATUSES = {"backgrounded", "running", "awaiting_input", "reconciling", "degraded"}
_MAX_ANNOUNCED_KEYS = 8_192


def create_mailbox_watch_loop(
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> Callable[[], Any]:
    announced: set[str] = set()
    trace_cursors: dict[str, int] = {}
    parent_ids: dict[str, str | None] = {}

    async def _parent_session_id(task_id: str) -> str | None:
        # Trace events must route on the PARENT session: attached parent
        # runtimes subscribe filtered by their own session id.
        if task_id not in parent_ids:
            meta = await readTaskMeta(task_id) or {}
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

    async def sweep_once() -> int:
        emitted = 0
        for record in await listTaskRecords():
            if record.get("kind") != "subagent" or record.get("status") not in _WATCHED_TASK_STATUSES:
                continue
            task_id = record.get("id") or record.get("taskId")
            if not isinstance(task_id, str) or not task_id:
                continue
            emitted += await forward_trace_events(task_id)
            if not os.path.exists(task_mailbox_path(task_id)):
                continue
            for message in await read_task_mailbox_messages(task_id):
                if message.get("direction") != "to_parent" or message.get("status") != "queued":
                    continue
                key = f"{task_id}:{message.get('messageId')}"
                if key in announced:
                    continue
                if len(announced) >= _MAX_ANNOUNCED_KEYS:
                    announced.clear()
                announced.add(key)
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
