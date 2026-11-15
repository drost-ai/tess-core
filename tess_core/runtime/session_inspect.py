"""What the runtime owns, in one call: identifiers and lifecycle reasons
only, never task output or transcripts.

``runtime.status()`` is synchronous and answers from memory: the daemon and
who owns it, which sessions are open and why, which closed sessions have
pending work and which reopens the host refused, the monitor intake's
counters, unfinished cleanup, and what caused the most recent open, close,
spawn and stop. ``await runtime.inspect()`` adds what lives on disk: every
session's children (working / idle with age and time left / stopped with
the reason, and whether a message or question waits for them) and every
monitor delivery (pending, claimed, authored, retryable, failed; paused
while muted; uncertain while automatic continuation is blocked).
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from .._utils import now_iso
from ..storage import list_child_agents, list_task_records
from ..tasks.communication import read_task_mailbox_messages, read_task_questions, task_mailbox_path, task_questions_path
from ..tasks.monitor_delivery import MonitorDeliveryController

_LIVE_CHILD_STATUSES = {"starting", "queued", "running", "backgrounded", "awaiting_input"}


def _age_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return max(0.0, datetime.now().timestamp() - stamp)


async def inspect_children(session_ids: set[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Children by parent session: what each is doing, with ids and reasons."""
    from ..config._knobs import knob_value

    ttl_seconds = int(knob_value("subagents.resident_idle_ttl_ms")) / 1000
    tasks_by_agent = {
        record.get("sourceId"): record
        for record in await list_task_records()
        if record.get("kind") == "subagent" and isinstance(record.get("sourceId"), str)
    }
    children: dict[str, list[dict[str, Any]]] = {}
    for child in await list_child_agents():
        parent = child.get("parentSessionId")
        if not isinstance(parent, str) or (session_ids is not None and parent not in session_ids):
            continue
        task = tasks_by_agent.get(child.get("agentId"))
        task_id = task.get("id") if task else None
        entry: dict[str, Any] = {
            "agentId": child.get("agentId"),
            "name": child.get("name"),
            "sessionId": child.get("sessionId"),
            "taskId": task_id,
            "status": child.get("status"),
            "pid": child.get("pid"),
        }
        if child.get("status") in _LIVE_CHILD_STATUSES:
            idle = _age_seconds(child.get("lastUpdatedAt")) if child.get("status") == "awaiting_input" else None
            if idle is not None:
                entry.update(state="idle", idleSeconds=round(idle, 1), timeLeftSeconds=round(max(0.0, ttl_seconds - idle), 1))
            else:
                entry["state"] = "working"
            entry["queuedMessages"] = 0
            entry["pendingQuestions"] = 0
            if isinstance(task_id, str):
                if os.path.exists(task_mailbox_path(task_id)):
                    entry["queuedMessages"] = sum(
                        1 for m in await read_task_mailbox_messages(task_id) if m.get("status") == "queued" and m.get("direction") == "to_child"
                    )
                if os.path.exists(task_questions_path(task_id)):
                    entry["pendingQuestions"] = sum(1 for q in await read_task_questions(task_id) if q.get("status") == "pending")
        else:
            entry.update(state="stopped", reason=child.get("stopReason") or child.get("completionReason"), completedAt=child.get("completedAt"))
        children.setdefault(parent, []).append(entry)
    return children


async def inspect_monitors(runtime_home: str) -> list[dict[str, Any]]:
    """Every monitor delivery the outbox holds, with its lifecycle state."""
    controller = MonitorDeliveryController(runtime_home)
    deliveries = []
    for item in await controller.list_pending(limit=1000):
        uncertain = bool(item.get("continuationBlocked")) or item.get("error") == "monitor.execution_uncertain"
        deliveries.append(
            {
                "monitorId": item["monitorId"],
                "sessionId": item["sessionId"],
                "deliveryId": item["deliveryId"],
                "mode": item["mode"],
                "state": item["state"],
                "phase": "paused" if item["muted"] else "uncertain" if uncertain else item["deliveryStatus"],
                "reason": item["reason"],
                "attempts": item["attempts"],
                "consumer": item.get("consumer"),
                "error": item.get("error"),
            }
        )
    return deliveries


async def inspect_runtime(runtime: Any) -> dict[str, Any]:
    status = runtime.status()
    return {
        **status,
        "children": await inspect_children(),
        "monitorDeliveries": await inspect_monitors(runtime.home),
        "observedAt": now_iso(),
    }
