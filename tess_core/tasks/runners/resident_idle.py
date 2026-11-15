"""The idle limit of a resident sub-agent.

A resident child answers its parent's messages for as long as it lives, but it
must not live forever: an idle child still holds a process, and whatever keeps
that process alive. After ``subagents.resident_idle_ttl_ms`` of idleness it
stops for good. There is no hibernation and no reactivation: every result it
produced has already been delivered, its transcript stays on disk, and a
parent that wants more starts a new child.

Idle means all of: no turn running, no message for it queued, no question of
its own still waiting for an answer, and no task or sub-agent of its own still
running. Working time never counts, and the limit is not an execution
deadline. The last check happens under the mailbox's record lock (see
``mailbox_closure``), so a parent's message either arrives in time and keeps
the child alive, or fails clearly.
"""
from __future__ import annotations

import time
from typing import Any

from ..._utils import now_iso
from ...storage import list_child_agents, list_task_records, mutate_child_agent, mutate_task_meta
from ..communication import append_task_event, read_task_questions, task_mailbox_path
from ..mailbox_closure import close_mailbox_if_idle

IDLE_EXPIRED = "idle_expired"
IDLE_EXPIRED_SUMMARY = "Stopped after its idle limit; every result was already delivered."

_LIVE_TASK_STATUSES = {"queued", "running", "backgrounded", "awaiting_input", "reconciling", "degraded"}
_LIVE_CHILD_STATUSES = {"queued", "running", "backgrounded", "awaiting_input"}
_TERMINAL_META_STATUSES = {"completed", "failed", "terminated", "stopped", "cancelled", "truncated"}
# While the limit has passed but something still holds the child (an open
# question, a task of its own), look again this often rather than every poll.
_RECHECK_SECONDS = 30.0


class ResidentIdleClock:
    """Measures idleness. It starts when a turn's result has been published and
    restarts whenever the child was seen to have something to do."""

    def __init__(self, ttl_ms: int) -> None:
        self.ttl_seconds = max(0.0, ttl_ms / 1000)
        self._idle_since = time.monotonic()
        self._next_check = 0.0

    def restart(self) -> None:
        self._idle_since = time.monotonic()
        self._next_check = 0.0

    def due(self) -> bool:
        now = time.monotonic()
        return now - self._idle_since >= self.ttl_seconds and now >= self._next_check

    def still_busy(self) -> None:
        """Something of the child's own is still open. It cannot have been idle
        before now, so the limit counts from here."""
        self._idle_since = time.monotonic()
        self._next_check = self._idle_since + min(_RECHECK_SECONDS, self.ttl_seconds)


async def _holds_work(task_id: str, session_id: str) -> str | None:
    """What of its own still keeps this child from being idle, if anything."""
    if any(question.get("status") == "pending" for question in await read_task_questions(task_id)):
        return "open_question"
    for record in await list_task_records():
        if record.get("sessionId") != session_id or (record.get("id") or record.get("taskId")) == task_id:
            continue
        if record.get("status") in _LIVE_TASK_STATUSES:
            return "own_task"
    for child in await list_child_agents():
        if child.get("parentSessionId") == session_id and child.get("status") in _LIVE_CHILD_STATUSES:
            return "descendant"
    return None


async def expire_if_idle(task_id: str, agent_id: str, session_id: str, clock: ResidentIdleClock) -> bool:
    """Stop the child for good if it is truly idle. Returns True when the
    worker should now exit; False when it still has something to do."""
    if await _holds_work(task_id, session_id):
        clock.still_busy()
        return False
    if not await close_mailbox_if_idle(task_mailbox_path(task_id), task_id, session_id, IDLE_EXPIRED):
        # A message arrived in time. The loop picks it up on its next poll.
        clock.restart()
        return False
    await _record_idle_expiry(task_id, agent_id, session_id, clock)
    return True


async def _record_idle_expiry(task_id: str, agent_id: str, session_id: str, clock: ResidentIdleClock) -> None:
    now = now_iso()

    def _stop_child(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if not current or current.get("status") not in _LIVE_CHILD_STATUSES:
            return current  # first terminal wins
        return {
            **current,
            "status": "stopped",
            "waitingReason": None,
            "stopReason": IDLE_EXPIRED,
            "completionReason": IDLE_EXPIRED,
            "completedAt": now,
            "pid": None,
            "stoppable": False,
            "retryable": False,
            "lastUpdatedAt": now,
        }

    def _stop_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
        if not meta or meta.get("status") in _TERMINAL_META_STATUSES:
            return meta  # first terminal wins
        # The worker owns this terminal state; the supervisor projects it into
        # the task record and announces task.stopped when the process exits,
        # exactly as it does for a one-shot child.
        return {
            **meta,
            "status": "stopped",
            "waitingReason": None,
            "detailStatus": IDLE_EXPIRED,
            "stopReason": IDLE_EXPIRED,
            "completionReason": IDLE_EXPIRED,
            "summary": meta.get("resultSummary") or meta.get("summary") or IDLE_EXPIRED_SUMMARY,
            "completedAt": now,
            "finishedAt": now,
            "updatedAt": now,
            "stoppable": False,
            "retryable": False,
        }

    await mutate_child_agent(agent_id, _stop_child)
    await mutate_task_meta(task_id, _stop_meta)
    await append_task_event(
        task_id,
        {
            "type": "task.subagent.idle_expired",
            "agentId": agent_id,
            "childSessionId": session_id,
            "idleTtlMs": int(clock.ttl_seconds * 1000),
        },
    )
