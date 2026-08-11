"""Daemon-side monitor observation engine.

The session-side monitor runtime was only ever constructed when the daemon
was OFF, and the daemon is default-on — so in production TaskMonitorStart
returned "active" and no tick ever fired, while a second, weaker
terminalizer in live_work rewrote monitors with no revision, no
transitionId, and no event (and terminalized muted monitors).

Observation belongs to the daemon: it watches task records and commits
status transitions through the CAS monitor transaction (revision +
transitionId + identity guards), emitting monitor.status_changed to every
subscribed session. Narration — LLM-authored prose appended to the live
session — stays in the session process, driven by the monitor.narration_due
events this engine emits; a daemon thread must never mutate a live session.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from ..._utils import maybe_await
from ...storage import list_monitors
from ..monitor_observation import buildMonitorObservationMutation
from ..monitor_transactions import (
    MonitorTransactionError,
    commitMonitorMutation,
    monitorTransitionEventFields,
)
from ..monitors import _get_output_size, _load_task_record, _should_check_monitor


async def tick_daemon_monitors(
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> int:
    """One observation pass over every session's checkable monitors.

    Returns the number of event-bearing transitions committed."""
    now_ms = int(time.time() * 1000)
    transitions = 0
    for monitor in await list_monitors():
        if not _should_check_monitor(monitor, now_ms):
            continue
        monitor_id = monitor.get("id")
        session_id = monitor.get("sessionId")
        if not isinstance(monitor_id, str) or not isinstance(session_id, str):
            continue
        task = await _load_task_record(monitor.get("taskId"))
        output_size = _get_output_size(monitor.get("taskId"))
        try:
            observed = await commitMonitorMutation(
                monitorId=monitor_id,
                expectedSessionId=session_id,
                mutate=lambda latest, task=task, size=output_size: buildMonitorObservationMutation(
                    latest, task, size, now_ms
                ),
            )
        except MonitorTransactionError:
            continue
        next_monitor = observed["current"]
        if observed.get("transitionId"):
            transitions += 1
            await maybe_await(
                emit(
                    "monitor.status_changed",
                    {
                        "session_id": session_id,
                        "monitor_id": next_monitor.get("id"),
                        "task_id": next_monitor.get("taskId"),
                        "status": next_monitor.get("status"),
                        "previous_status": observed["previous"].get("status"),
                        "changed": observed.get("statusChanged", False),
                        "task_status": next_monitor.get("lastObservedTaskStatus"),
                        "exit_code": (task or {}).get("exitCode"),
                        **monitorTransitionEventFields(observed),
                    },
                    session_id,
                )
            )
        reason = (observed.get("value") or {}).get("reason")
        if reason and not next_monitor.get("muted"):
            # The narration channel: the owning session authors the prose
            # where its LLM config and message loop live.
            await maybe_await(
                emit(
                    "monitor.narration_due",
                    {
                        "session_id": session_id,
                        "monitor_id": next_monitor.get("id"),
                        "task_id": next_monitor.get("taskId"),
                        "status": next_monitor.get("status"),
                        "reason": reason,
                        "output_size": output_size,
                        "last_narrated_output_size": int(next_monitor.get("lastNarratedOutputSize") or 0),
                    },
                    session_id,
                )
            )
    return transitions
