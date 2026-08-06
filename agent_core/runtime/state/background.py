"""State reduction for background-work events: tasks, health checks,
monitors, collaboration, remote sessions, and bridges."""
from __future__ import annotations

from typing import Any

from .shared import (
    _collaboration_worker_record,
    _field,
    _summarize_tasks,
    _task_from_event,
    _task_from_snapshot_record,
    _task_patch_from_event,
    _unique_push,
    _update_runtime_task,
    _upsert_runtime_task,
)

BACKGROUND_RUNTIME_STATE_EVENTS = {
    "task.snapshot",
    "task.registered",
    "task.restored",
    "task.updated",
    "task.output_delta",
    "task.completed",
    "task.failed",
    "health_check.unhealthy",
    "health_check.recovered",
    "monitor.snapshot",
    "monitor.created",
    "monitor.assistant_update",
    "collaboration.snapshot",
    "collaboration.assistant_update",
    "bridge.message",
    "remote.status_changed",
    "bridge.status_changed",
    "bridge.event",
    "remote.event",
}

def reduce_background_runtime_state(
    next_state: dict[str, Any], typ: str, data: dict[str, Any], timestamp: Any
) -> dict[str, Any]:
    if typ == "task.snapshot":
        tasks = [_task_from_snapshot_record(task, next_state.get("session", {}).get("activeSessionId"), timestamp) for task in (_field(data, "tasks") or [])]
        next_state["tasks"] = _summarize_tasks(
            {
                "total": _field(data, "total", default=len(tasks)),
                "tasks": tasks,
                "activeCount": _field(data, "active", "activeCount", default=0),
                "backgroundedCount": _field(data, "backgrounded", "backgroundedCount", default=0),
                "awaitingInputCount": _field(data, "awaitingInput", "awaiting_input", "awaitingInputCount", default=0),
                "failedCount": _field(data, "failed", "failedCount", default=0),
                "labels": _field(data, "labels") or [],
                "lastUpdatedAt": timestamp,
            }
        )
    elif typ in ("task.registered", "task.restored"):
        task_id = _field(data, "taskId", "task_id", "id")
        if task_id:
            status = _field(data, "status") or ("idle" if typ == "task.restored" else "running")
            next_state = _upsert_runtime_task(next_state, _task_from_event(data, task_id, status, next_state.get("session", {}).get("activeSessionId"), timestamp, created=True))
    elif typ == "task.updated":
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id", "id"), _task_patch_from_event(data, _field(data, "status"), timestamp))
    elif typ == "task.output_delta":
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id", "id"), {"outputCursor": _field(data, "outputCursor", "output_cursor"), "updatedAt": timestamp})
    elif typ == "task.completed":
        patch = _task_patch_from_event(data, "completed", timestamp)
        patch["completedAt"] = timestamp
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id", "id"), patch)
    elif typ == "task.failed":
        patch = _task_patch_from_event(data, "failed", timestamp)
        patch["completedAt"] = timestamp
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id", "id"), patch)
    elif typ == "health_check.unhealthy":
        title = _field(data, "title") or "Health check"
        error = _field(data, "error")
        summary = f"{title} is unhealthy: {error}" if error else f"{title} is unhealthy."
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id"), {"kind": "health_check", "status": "degraded", "title": _field(data, "title"), "summary": summary, "updatedAt": timestamp})
    elif typ == "health_check.recovered":
        title = _field(data, "title") or "Health check"
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id"), {"kind": "health_check", "status": "running", "title": _field(data, "title"), "summary": f"{title} recovered and is running again.", "updatedAt": timestamp})
    elif typ == "monitor.snapshot":
        next_state["monitors"] = {
            **next_state.get("monitors", {}),
            "snapshot": {
                "total": _field(data, "total"),
                "active": _field(data, "active"),
                "failed": _field(data, "failed"),
                "stopped": _field(data, "stopped"),
                "muted": _field(data, "muted"),
                "nextCheckInMs": _field(data, "nextCheckInMs", "next_check_in_ms"),
                "labels": _field(data, "labels") or [],
            },
        }
    elif typ == "monitor.created":
        monitor_id = _field(data, "monitorId", "monitor_id")
        if monitor_id:
            active = _unique_push(next_state.get("monitors", {}).get("activeMonitorIds", []), monitor_id)
            next_state["monitors"] = {**next_state.get("monitors", {}), "activeMonitorIds": active}
    elif typ == "monitor.assistant_update":
        updates = [
            {
                "monitorId": _field(data, "monitorId", "monitor_id"),
                "taskId": _field(data, "taskId", "task_id"),
                "status": _field(data, "status"),
                "reason": _field(data, "reason"),
                "text": _field(data, "text") or _field(data, "message") or "",
                "timestamp": timestamp,
            },
            *next_state.get("monitors", {}).get("recentAssistantUpdates", []),
        ]
        next_state["monitors"] = {**next_state.get("monitors", {}), "recentAssistantUpdates": updates[:10]}
    elif typ == "collaboration.snapshot":
        collab = next_state.get("collaboration", {})
        next_state["collaboration"] = {
            **collab,
            "workers": [_collaboration_worker_record(worker, timestamp) for worker in (_field(data, "workers") or [])],
            "activeCount": _field(data, "activeCount", "active_count", default=0),
            "awaitingInputCount": _field(data, "awaitingInputCount", "awaiting_input_count", default=0),
            "backgroundCount": _field(data, "backgroundCount", "background_count", default=0),
            "remoteStatus": _field(data, "remoteStatus", "remote_status"),
            "remoteTitle": _field(data, "remoteTitle", "remote_title"),
            "bridgeStatus": _field(data, "bridgeStatus", "bridge_status"),
            "pendingBridgeMessages": _field(data, "pendingBridgeMessages", "pending_bridge_messages"),
            "labels": _field(data, "labels") or [],
        }
    elif typ == "collaboration.assistant_update":
        updates = [
            {
                "sourceKind": _field(data, "sourceKind", "source_kind"),
                "sourceId": _field(data, "sourceId", "source_id"),
                "status": _field(data, "status"),
                "label": _field(data, "label"),
                "text": _field(data, "text") or "",
                "timestamp": timestamp,
            },
            *next_state.get("collaboration", {}).get("recentAssistantUpdates", []),
        ]
        next_state["collaboration"] = {**next_state.get("collaboration", {}), "recentAssistantUpdates": updates[:10]}
    elif typ == "bridge.message":
        bridge_id = _field(data, "bridgeId", "bridge_id") or next_state.get("bridge", {}).get("bridgeId")
        remote_session_id = _field(data, "remoteSessionId", "remote_session_id") or next_state.get("bridge", {}).get("remoteSessionId")
        next_state["bridge"] = {
            "attached": True,
            "bridgeId": bridge_id,
            "remoteSessionId": remote_session_id,
            "lastMessageText": _field(data, "text") or _field(data, "message"),
            "lastMessageAt": timestamp,
            "lastEventName": typ,
            "lastEventAt": timestamp,
        }
        next_state["collaboration"] = {**next_state.get("collaboration", {}), "pendingBridgeMessages": max(1, int(next_state.get("collaboration", {}).get("pendingBridgeMessages") or 0))}
    elif typ == "remote.status_changed":
        remote_session_id = _field(data, "remoteSessionId", "remote_session_id") or next_state.get("bridge", {}).get("remoteSessionId")
        bridge_id = _field(data, "bridgeId", "bridge_id") or next_state.get("bridge", {}).get("bridgeId")
        bridge_status = _field(data, "bridgeStatus", "bridge_status")
        unread = _field(data, "unreadBridgeMessages", "unread_bridge_messages")
        next_state["bridge"] = {
            **next_state.get("bridge", {}),
            "attached": bool(bridge_id) or next_state.get("bridge", {}).get("attached"),
            "bridgeId": bridge_id,
            "remoteSessionId": remote_session_id,
            "lastEventName": typ,
            "lastEventAt": timestamp,
        }
        next_state["collaboration"] = {
            **next_state.get("collaboration", {}),
            "remoteStatus": _field(data, "status") or next_state.get("collaboration", {}).get("remoteStatus"),
            "remoteTitle": _field(data, "title") or next_state.get("collaboration", {}).get("remoteTitle"),
            "bridgeStatus": bridge_status or next_state.get("collaboration", {}).get("bridgeStatus"),
            **({"pendingBridgeMessages": unread} if unread is not None else {}),
        }
    elif typ == "bridge.status_changed":
        status = _field(data, "status")
        pending = _field(data, "pendingMessages", "pending_messages")
        event_name = _field(data, "lastEventName", "last_event_name") or typ
        next_state["bridge"] = {
            **next_state.get("bridge", {}),
            "attached": False if status == "closed" else True,
            "bridgeId": _field(data, "bridgeId", "bridge_id") or next_state.get("bridge", {}).get("bridgeId"),
            "remoteSessionId": _field(data, "remoteSessionId", "remote_session_id") or next_state.get("bridge", {}).get("remoteSessionId"),
            "lastEventName": event_name,
            "lastEventAt": timestamp,
        }
        next_state["collaboration"] = {
            **next_state.get("collaboration", {}),
            "bridgeStatus": status or next_state.get("collaboration", {}).get("bridgeStatus"),
            **({"pendingBridgeMessages": pending} if pending is not None else {}),
        }
    elif typ in ("bridge.event", "remote.event"):
        event_name = _field(data, "eventName", "event_name", "event") or typ
        bridge = next_state.get("bridge", {})
        if typ == "bridge.event":
            next_state["bridge"] = {
                **bridge,
                "attached": False if event_name == "bridge.closed" else True,
                "bridgeId": _field(data, "bridgeId", "bridge_id") or bridge.get("bridgeId"),
                "remoteSessionId": _field(data, "remoteSessionId", "remote_session_id") or bridge.get("remoteSessionId"),
                "lastEventName": event_name,
                "lastEventAt": timestamp,
            }
            next_state["collaboration"] = {**next_state.get("collaboration", {}), "bridgeStatus": "closed" if event_name == "bridge.closed" else next_state.get("collaboration", {}).get("bridgeStatus")}
        else:
            next_state["bridge"] = {**bridge, "remoteSessionId": _field(data, "remoteSessionId", "remote_session_id") or bridge.get("remoteSessionId"), "lastEventName": event_name, "lastEventAt": timestamp}
            remote_status = "active" if event_name in {"remote.session_created", "remote.session_resumed"} else next_state.get("collaboration", {}).get("remoteStatus")
            next_state["collaboration"] = {**next_state.get("collaboration", {}), "remoteStatus": remote_status}
    return next_state
