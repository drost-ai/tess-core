"""Shared daemon/local observation: commit status and delivery intent together.

Events are latency hints. Hosts recover work from the durable monitor outbox,
including when no subscription or session lane existed at observation time.
"""
from __future__ import annotations

import time
from typing import Any, Callable
from pathlib import Path

from ..._utils import maybe_await
from ...storage import list_monitors, task_output_path
from ..monitor_observation import build_monitor_observation_mutation
from ..monitor_transactions import (
    MonitorTransactionError,
    commit_monitor_mutation,
    monitor_transition_event_fields,
)
from ..monitors import _load_task_record, _should_check_monitor


async def tick_daemon_monitors(
    emit: Callable[[str, dict[str, Any], str | None], Any],
    *, session_id: str | None = None,
) -> int:
    """One observation pass over every session's checkable monitors.

    Returns the number of event-bearing transitions committed."""
    now_ms = int(time.time() * 1000)
    transitions = 0
    for monitor in await list_monitors():
        if session_id is not None and monitor.get("sessionId") != session_id:
            continue
        if not _should_check_monitor(monitor, now_ms):
            continue
        monitor_id = monitor.get("id")
        native_session_id = monitor.get("sessionId")
        if not isinstance(monitor_id, str) or not isinstance(native_session_id, str):
            continue
        async def observe(latest):
            if not _should_check_monitor(latest, now_ms):
                return {"record": latest, "changed": False, "value": {}}
            task = await _load_task_record(latest.get("taskId"))
            try:
                stat = Path(task_output_path(latest["taskId"])).stat()
                size, identity = stat.st_size, f"{stat.st_dev}:{stat.st_ino}"
            except FileNotFoundError:
                size, identity = 0, "missing"
            return build_monitor_observation_mutation(latest, task, size, now_ms, identity)
        try:
            observed = await commit_monitor_mutation(
                monitorId=monitor_id,
                expectedSessionId=native_session_id,
                mutate=observe,
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
                        "session_id": native_session_id,
                        "monitor_id": next_monitor.get("id"),
                        "task_id": next_monitor.get("taskId"),
                        "status": next_monitor.get("status"),
                        "previous_status": observed["previous"].get("status"),
                        "changed": observed.get("statusChanged", False),
                        "task_status": next_monitor.get("lastObservedTaskStatus"),
                        "exit_code": ((next_monitor.get("delivery") or {}).get("queue") or [{}])[-1].get("exitCode"),
                        **monitor_transition_event_fields(observed),
                    },
                    native_session_id,
                )
            )
        reason = (observed.get("value") or {}).get("reason")
        if reason and not next_monitor.get("muted"):
            # Compatibility hint only; durable pending state is authoritative.
            await maybe_await(
                emit(
                    "monitor.narration_due",
                    {
                        "session_id": native_session_id,
                        "monitor_id": next_monitor.get("id"),
                        "task_id": next_monitor.get("taskId"),
                        "status": next_monitor.get("status"),
                        "reason": reason,
                        "output_size": next_monitor.get("lastObservedOutputSize", 0),
                        "last_narrated_output_size": int(next_monitor.get("lastNarratedOutputSize") or 0),
                    },
                    native_session_id,
                )
            )
    return transitions
