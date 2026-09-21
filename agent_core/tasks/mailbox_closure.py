"""Closing a child's mailbox, atomically with respect to whoever sends to it.

A resident child that has been idle long enough stops for good. The danger is
the last instant: a parent's message appended just as the child decides to go
would sit in a mailbox nobody will ever read again. Both sides therefore meet
under the mailbox's own record lock (the transaction ``append_json_line`` and
``mutate_json_lines`` share, across processes):

- the child closes its mailbox only if no message for it is queued, in the
  same transaction that checks;
- a sender appends only if the mailbox is not closed, in the same transaction
  that checks, and otherwise gets :class:`TaskMailboxClosedError`.

So a message either arrives in time and keeps the child alive, or fails
clearly. It is never silently lost.
"""
from __future__ import annotations

import uuid
from typing import Any

from .._utils import now_iso
from ..storage.json_io import mutate_json_lines, read_json_lines

MAILBOX_CLOSED_KIND = "mailbox_closed"


class TaskMailboxClosedError(RuntimeError):
    """The child behind this mailbox has stopped and will never read it."""

    def __init__(self, task_id: str, reason: str | None) -> None:
        self.task_id = task_id
        self.reason = reason or "stopped"
        super().__init__(f"Task {task_id} no longer accepts messages ({self.reason}).")


def is_mailbox_closure(record: Any) -> bool:
    return isinstance(record, dict) and record.get("kind") == MAILBOX_CLOSED_KIND


def _closure(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((record for record in reversed(records) if is_mailbox_closure(record)), None)


async def read_mailbox_closure(mailbox_path: str) -> dict[str, Any] | None:
    return _closure(await read_json_lines(mailbox_path))


async def append_message_unless_closed(mailbox_path: str, task_id: str, message: dict[str, Any]) -> None:
    def _append(records: list[dict[str, Any]]) -> dict[str, Any]:
        closed = _closure(records)
        if closed is not None:
            raise TaskMailboxClosedError(task_id, closed.get("reason"))
        return {"records": [*records, message], "result": None, "changed": True}

    await mutate_json_lines(mailbox_path, _append)


async def close_mailbox_if_idle(mailbox_path: str, task_id: str, child_session_id: str, reason: str) -> bool:
    """Close the mailbox unless a message for the child is still queued.

    Returns True when the mailbox is closed (now or already), False when a
    queued message means the child still has work to do."""

    def _close(records: list[dict[str, Any]]) -> dict[str, Any]:
        if _closure(records) is not None:
            return {"records": records, "result": True, "changed": False}
        latest: dict[str, dict[str, Any]] = {}
        for record in records:
            message_id = record.get("messageId")
            if isinstance(message_id, str):
                latest[message_id] = record
        waiting = any(
            message.get("status") == "queued"
            and message.get("direction") in (None, "to_child")
            and message.get("childSessionId") in (None, child_session_id)
            for message in latest.values()
        )
        if waiting:
            return {"records": records, "result": False, "changed": False}
        closed = {
            "messageId": f"closed-{uuid.uuid4()}",
            "taskId": task_id,
            "direction": "control",
            "kind": MAILBOX_CLOSED_KIND,
            "childSessionId": child_session_id,
            "status": "closed",
            "reason": reason,
            "createdAt": now_iso(),
        }
        return {"records": [*records, closed], "result": True, "changed": True}

    return bool(await mutate_json_lines(mailbox_path, _close))
