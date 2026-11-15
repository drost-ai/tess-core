"""Stop everything one session owns: its tasks, its children and their
descendants, through the existing task stop machinery. Never another
session, never the daemon.

Ownership is read from the records: a task belongs to the session named in
its record, a child agent to its ``parentSessionId``, and a sub-agent task
names the child's own session, whose tasks and children are stopped in turn.
Each task goes through ``stopTask``, the one containment ladder there is
(TERM then KILL, exit verified), and its records then say why:
``session_ended``. The reason is written before the stop, because the exit
finalizer spreads what it reads, and the terminal state is settled after it,
because that finalizer calls a killed sub-agent ``failed`` where the truth
is a deliberate stop.
"""
from __future__ import annotations

from typing import Any

from .._utils import now_iso
from ..storage import list_child_agents, list_task_records, mutate_child_agent, mutate_task_meta
from ..tasks.communication import append_task_event

SESSION_ENDED = "session_ended"
RUNTIME_STOPPED = "runtime_stopped"
_LIVE_CHILD_STATUSES = {"starting", "queued", "running", "backgrounded", "awaiting_input"}
_TERMINAL_TASK_STATUSES = {"completed", "failed", "stopped", "terminated", "cancelled", "truncated"}
_WORKER_KINDS = {"background_command", "main_session", "subagent"}


async def stop_all_workers(supervisor: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """The runtime is stopping (or died): every worker the daemon owns is
    stopped through the same ladder, its records saying ``runtime_stopped``,
    never resumed. Stopping is not undoing: the record states that the
    process ended and claims nothing about the effects of tools that were
    running (their uncertainty metadata is kept), and never marks such work
    safe to retry."""
    reason = str(params.get("reason") or RUNTIME_STOPPED)
    records = await list_task_records()
    children = await list_child_agents()
    stopped_tasks: list[str] = []
    failures: list[dict[str, Any]] = []
    live_children = [child for child in children if child.get("status") in _LIVE_CHILD_STATUSES]
    for child in live_children:
        await mutate_child_agent(child["agentId"], lambda record, reason=reason: _annotate(record, reason))
    for task in records:
        task_id = task.get("id")
        if not isinstance(task_id, str) or task.get("kind") not in _WORKER_KINDS or task.get("status") in _TERMINAL_TASK_STATUSES:
            continue
        try:
            await mutate_task_meta(task_id, lambda meta, reason=reason: _annotate(meta, reason))
            await supervisor["stopTask"](task_id)
            await mutate_task_meta(task_id, lambda meta, reason=reason: _annotate(meta, reason))
            await append_task_event(task_id, {"type": "task.runtime_stopped", "reason": reason})
            stopped_tasks.append(task_id)
        except Exception as error:
            failures.append({"taskId": task_id, "error": str(error)[:256]})
    for child in live_children:
        await mutate_child_agent(child["agentId"], lambda record, reason=reason: _settle_child(record, reason))
    return {"reason": reason, "stoppedTasks": stopped_tasks, "stoppedChildren": [child["agentId"] for child in live_children], "failures": failures}


async def stop_session(supervisor: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    session_id = params.get("sessionId") or params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("stopSession requires a sessionId")
    reason = str(params.get("reason") or SESSION_ENDED)
    records = await list_task_records()
    children = await list_child_agents()
    sessions = [session_id]
    seen = {session_id}
    stopped_tasks: list[str] = []
    stopped_children: list[str] = []
    failures: list[dict[str, Any]] = []
    index = 0
    while index < len(sessions):
        current = sessions[index]
        index += 1
        live_children = [child for child in children if child.get("parentSessionId") == current and child.get("status") in _LIVE_CHILD_STATUSES]
        for child in children:
            child_session = child.get("sessionId")
            if child.get("parentSessionId") == current and isinstance(child_session, str) and child_session not in seen:
                seen.add(child_session)
                sessions.append(child_session)
        for child in live_children:
            await mutate_child_agent(child["agentId"], lambda record, reason=reason: _annotate(record, reason))
        for task in records:
            if task.get("sessionId") != current:
                continue
            task_id = task.get("id")
            child_session = task.get("attachableSessionId")
            if task.get("kind") == "subagent" and isinstance(child_session, str) and child_session not in seen:
                seen.add(child_session)
                sessions.append(child_session)
            if not isinstance(task_id, str) or task.get("status") in _TERMINAL_TASK_STATUSES:
                continue
            try:
                await mutate_task_meta(task_id, lambda meta, reason=reason: _annotate(meta, reason))
                await supervisor["stopTask"](task_id)
                await mutate_task_meta(task_id, lambda meta, reason=reason: _annotate(meta, reason))
                await append_task_event(task_id, {"type": "task.session_ended", "sessionId": current, "reason": reason})
                stopped_tasks.append(task_id)
            except Exception as error:
                failures.append({"taskId": task_id, "error": str(error)[:256]})
        for child in live_children:
            await mutate_child_agent(child["agentId"], lambda record, reason=reason: _settle_child(record, reason))
            stopped_children.append(child["agentId"])
    return {
        "sessionId": session_id,
        "reason": reason,
        "sessions": sessions,
        "stoppedTasks": stopped_tasks,
        "stoppedChildren": stopped_children,
        "failures": failures,
    }


def _annotate(record: dict[str, Any] | None, reason: str) -> dict[str, Any] | None:
    if not record:
        return record
    return {**record, "stopReason": reason, "completionReason": reason}


def _settle_child(record: dict[str, Any] | None, reason: str) -> dict[str, Any] | None:
    """The child was stopped on purpose: its record says stopped, not failed."""
    if not record:
        return record
    now = now_iso()
    return {
        **record,
        "status": "stopped",
        "waitingReason": None,
        "stopReason": reason,
        "completionReason": reason,
        "completedAt": record.get("completedAt") or now,
        "pid": None,
        "stoppable": False,
        "retryable": False,
        "lastUpdatedAt": now,
    }
