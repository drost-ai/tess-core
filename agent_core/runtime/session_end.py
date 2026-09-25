"""``end``: the one explicit instruction a session ever gets.

Closing is the default state and is never an event; ending is an instruction
(deleting a conversation, D5) and is never inferred from a timeout, a
disconnect or a shutdown. It is durable (an ``ended`` marker on the session
record, additive, with reason and time), it stops the session's children
and their descendants through the daemon and nothing else, it fails what
was still addressed to the session with a recorded reason rather than
dropping it, it closes the session if it was open (cancelling a running
turn first), and it blocks every reopen: handles raise :class:`SessionEnded`,
an automatic reopen is never attempted, the host is not even asked.

Order matters for a crash in the middle: the marker goes first, so whatever
else is left undone, the session never runs again; ``end`` is idempotent and
finishes the rest when called again.
"""
from __future__ import annotations

import os
from typing import Any

from .._utils import now_iso
from ..storage import list_monitors
from ..storage.session_archive import load_session, mutate_session_record
from ..tasks.communication import answer_task_question, read_task_questions, task_mailbox_path
from ..tasks.mailbox_closure import fail_queued_messages
from ..tasks.monitor_delivery import MonitorDeliveryController, MonitorDeliveryError
from .session_options import UnknownSessionError

SESSION_ENDED = "session_ended"


def ended_marker(record: dict[str, Any] | None) -> dict[str, Any] | None:
    metadata = record.get("metadata") if isinstance(record, dict) and isinstance(record.get("metadata"), dict) else {}
    marker = metadata.get("ended")
    return marker if isinstance(marker, dict) else None


async def end_session(manager: Any, session_id: str, reason: str) -> dict[str, Any]:
    record = await load_session(session_id)
    if record is None or record.get("id") != session_id:
        raise UnknownSessionError(session_id)
    outcome: dict[str, Any] = {
        "sessionId": session_id, "reason": reason, "alreadyEnded": False,
        "stopped": None, "failedMessages": 0, "answeredQuestions": 0, "stoppedMonitors": 0,
    }
    marker = ended_marker(record)
    if marker is not None:
        outcome.update(alreadyEnded=True, endedAt=marker.get("at"), reason=marker.get("reason"))
    else:
        marker = {"reason": reason, "at": now_iso()}
        outcome["endedAt"] = marker["at"]

    def mark(current: dict[str, Any]) -> None:
        current.setdefault("metadata", {})["ended"] = dict(marker)

    await mutate_session_record(session_id, mark)

    # The session itself: unloaded at once, a running turn cancelled, the
    # queue dropped, then its components released. The live record carries
    # the marker first, so a persist from memory during the cancel (the
    # engine's rollback write-back) cannot erase it; the disk is marked
    # again afterwards for the same reason.
    live = manager.live(session_id)
    if live is not None:
        mark(live.record)
        running = live.turns.running()
        if running is not None:
            live.turns.cancel(running.id)
        manager._live.pop(session_id, None)
        await manager.release(session_id, live, "ended")
        await mutate_session_record(session_id, mark)
    manager.pending.drop(session_id, "ended")

    # Its children and their descendants, through the daemon's stop ladder.
    proxy = manager.runtime.daemon_proxy()
    if proxy is not None:
        try:
            outcome["stopped"] = await proxy["stopSession"]({"sessionId": session_id, "reason": SESSION_ENDED})
        except Exception as error:
            outcome["stopError"] = str(error)[:256]

    # What was still addressed to it: failed with the reason, never dropped.
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for task_id in metadata.get("taskIds") or []:
        if not isinstance(task_id, str) or not task_id:
            continue
        mailbox = task_mailbox_path(task_id)
        if os.path.exists(mailbox):
            outcome["failedMessages"] += await fail_queued_messages(mailbox, task_id, "to_parent", SESSION_ENDED)
        for question in await read_task_questions(task_id):
            if question.get("status") == "pending":
                answered = await answer_task_question(task_id, str(question.get("questionId")), f"unanswered:{SESSION_ENDED}")
                if answered:
                    outcome["answeredQuestions"] += 1
    controller = MonitorDeliveryController(manager.runtime.home)
    for monitor in await list_monitors():
        if monitor.get("sessionId") == session_id and not monitor.get("deliveryStoppedAt"):
            try:
                await controller.stop(monitor["id"], session_id)
                outcome["stoppedMonitors"] += 1
            except MonitorDeliveryError:
                pass
    return outcome
