from __future__ import annotations


from typing import Any, Callable

from .._utils import now_iso
from ..storage import appendDaemonEvent, loadDaemonEvents
from .protocol import DAEMON_MAX_RESPONSE_FRAME_BYTES, encodeDaemonFrame

async def _publish_daemon_event(subscribers: dict[str, dict[str, Any]], payload: dict[str, Any]) -> None:
    stale: list[str] = []
    for subscriber_id, subscriber in list(subscribers.items()):
        if not _event_matches_session(payload, subscriber.get("sessionId")):
            continue
        target = subscriber.get("peer") or subscriber.get("writer")
        if not target:
            stale.append(subscriber_id)
            continue
        try:
            await _write_daemon_event(target, payload)
        except Exception:
            stale.append(subscriber_id)
    for subscriber_id in stale:
        subscriber = subscribers.pop(subscriber_id, None)
        target = (subscriber.get("peer") or subscriber.get("writer")) if subscriber else None
        closer = getattr(target, "close", None)
        if callable(closer):
            result = closer()
            if hasattr(result, "__await__"):
                await result

async def _write_daemon_event(target: Any, record: dict[str, Any]) -> None:
    payload = {"type": "event", "record": record}
    sender = getattr(target, "sendEvent", None)
    if callable(sender):
        if not await sender(payload):
            raise RuntimeError("Daemon peer rejected an event frame.")
        return
    target.write(encodeDaemonFrame(payload, DAEMON_MAX_RESPONSE_FRAME_BYTES))
    await target.drain()

async def _replay_daemon_events(instance_id: str, target: Any, session_id: str | None, after_cursor: int) -> None:
    for record in await loadDaemonEvents(instance_id):
        cursor = record.get("cursor")
        if not isinstance(cursor, int) or cursor <= after_cursor:
            continue
        if not _event_matches_session(record, session_id):
            continue
        await _write_daemon_event(target, record)

async def _send_bootstrap_snapshots(
    target: Any,
    supervisor: dict[str, Any],
    session_id: str | None,
    next_cursor: Callable[[], int],
) -> None:
    if not session_id:
        return
    bootstrap = await supervisor["getBootstrap"](session_id)
    task_snapshot = bootstrap.get("taskSnapshot") if isinstance(bootstrap, dict) else None
    monitor_snapshot = bootstrap.get("monitorSnapshot") if isinstance(bootstrap, dict) else None
    collaboration_snapshot = bootstrap.get("collaborationSnapshot") if isinstance(bootstrap, dict) else None
    if task_snapshot:
        await _write_daemon_event(target, _make_snapshot_record(next_cursor(), session_id, "task.snapshot", _task_snapshot_payload(session_id, task_snapshot)))
    if monitor_snapshot:
        await _write_daemon_event(target, _make_snapshot_record(next_cursor(), session_id, "monitor.snapshot", _monitor_snapshot_payload(session_id, monitor_snapshot)))
    if collaboration_snapshot:
        await _write_daemon_event(target, _make_snapshot_record(next_cursor(), session_id, "collaboration.snapshot", _collaboration_snapshot_payload(collaboration_snapshot)))

def _make_snapshot_record(cursor: int, session_id: str, event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"cursor": cursor, "createdAt": now_iso(), "sessionId": session_id, "event": event, "data": data}

def _task_snapshot_payload(session_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "total": snapshot.get("total", 0),
        "active": snapshot.get("active", 0),
        "backgrounded": snapshot.get("backgrounded", 0),
        "awaiting_input": snapshot.get("awaitingInput", snapshot.get("awaiting_input", 0)),
        "failed": snapshot.get("failed", 0),
        "labels": snapshot.get("labels", []),
        "tasks": [_task_snapshot_record(task) for task in snapshot.get("tasks", []) if isinstance(task, dict)],
    }

def _task_snapshot_record(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "kind": task.get("kind"),
        "status": task.get("status"),
        "title": task.get("title"),
        "summary": task.get("summary"),
        "updated_at": task.get("updatedAt"),
        "session_id": task.get("sessionId"),
        "foreground_mode": task.get("foregroundMode"),
        "task_family": task.get("taskFamily"),
        "detail_status": task.get("detailStatus"),
        "attachable_session_id": task.get("attachableSessionId"),
        "retrieve_only": task.get("retrieveOnly"),
        "result_summary": task.get("resultSummary"),
    }

def _monitor_snapshot_payload(session_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "total": snapshot.get("total", 0),
        "active": snapshot.get("active", 0),
        "failed": snapshot.get("failed", 0),
        "stopped": snapshot.get("stopped", 0),
        "muted": snapshot.get("muted", 0),
        "labels": snapshot.get("labels", []),
        "next_check_in_ms": snapshot.get("nextCheckInMs") or snapshot.get("next_check_in_ms"),
    }

def _collaboration_snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "workers": snapshot.get("workers", []),
        "active_count": snapshot.get("activeCount", snapshot.get("active_count", 0)),
        "awaiting_input_count": snapshot.get("awaitingInputCount", snapshot.get("awaiting_input_count", 0)),
        "background_count": snapshot.get("backgroundCount", snapshot.get("background_count", 0)),
        "remote_status": snapshot.get("remoteStatus"),
        "remote_title": snapshot.get("remoteTitle"),
        "bridge_status": snapshot.get("bridgeStatus"),
        "pending_bridge_messages": snapshot.get("pendingBridgeMessages"),
        "labels": snapshot.get("labels", []),
    }


def _event_matches_session(payload: dict[str, Any], session_id: str | None) -> bool:
    if not session_id:
        return True
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("sessionId") == session_id or data.get("sessionId") == session_id or data.get("session_id") == session_id


async def _record_daemon_lifecycle_event(instance_id: str, event: str, data: dict[str, Any], next_cursor: Callable[[], int]) -> None:
    await appendDaemonEvent(instance_id, {"cursor": next_cursor(), "createdAt": now_iso(), "event": event, "data": data})
