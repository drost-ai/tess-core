from __future__ import annotations

from .shared import *

async def _publish_daemon_event(subscribers: dict[str, dict[str, Any]], payload: dict[str, Any]) -> None:
    stale: list[str] = []
    for subscriber_id, subscriber in list(subscribers.items()):
        if not _event_matches_session(payload, subscriber.get("sessionId")):
            continue
        writer = subscriber.get("writer")
        if not writer:
            stale.append(subscriber_id)
            continue
        try:
            await _write_daemon_event(writer, payload)
        except Exception:
            stale.append(subscriber_id)
    for subscriber_id in stale:
        subscriber = subscribers.pop(subscriber_id, None)
        writer = subscriber.get("writer") if subscriber else None
        if writer:
            writer.close()

async def _write_daemon_event(writer: asyncio.StreamWriter, record: dict[str, Any]) -> None:
    writer.write(json.dumps({"type": "event", "record": record}, ensure_ascii=False).encode("utf-8") + b"\n")
    await writer.drain()

async def _replay_daemon_events(writer: asyncio.StreamWriter, session_id: str | None, after_cursor: int) -> None:
    for record in await loadDaemonEvents():
        cursor = record.get("cursor")
        if not isinstance(cursor, int) or cursor <= after_cursor:
            continue
        if not _event_matches_session(record, session_id):
            continue
        await _write_daemon_event(writer, record)

async def _send_bootstrap_snapshots(
    writer: asyncio.StreamWriter,
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
        await _write_daemon_event(writer, _make_snapshot_record(next_cursor(), session_id, "task.snapshot", _task_snapshot_payload(session_id, task_snapshot)))
    if monitor_snapshot:
        await _write_daemon_event(writer, _make_snapshot_record(next_cursor(), session_id, "monitor.snapshot", _monitor_snapshot_payload(session_id, monitor_snapshot)))
    if collaboration_snapshot:
        await _write_daemon_event(writer, _make_snapshot_record(next_cursor(), session_id, "collaboration.snapshot", _collaboration_snapshot_payload(collaboration_snapshot)))

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

async def _wait_for_subscription_close(reader: asyncio.StreamReader, shutdown: asyncio.Event) -> None:
    read_task = asyncio.create_task(reader.read())
    shutdown_task = asyncio.create_task(shutdown.wait())
    try:
        await asyncio.wait({read_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (read_task, shutdown_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(read_task, shutdown_task, return_exceptions=True)

def _event_matches_session(payload: dict[str, Any], session_id: str | None) -> bool:
    if not session_id:
        return True
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("sessionId") == session_id or data.get("sessionId") == session_id or data.get("session_id") == session_id

def _last_daemon_event_cursor(events: list[dict[str, Any]]) -> int:
    cursors = [event.get("cursor") for event in events if isinstance(event.get("cursor"), int)]
    return max(cursors, default=0)

async def _record_daemon_lifecycle_event(event: str, data: dict[str, Any], next_cursor: Callable[[], int]) -> None:
    await appendDaemonEvent({"cursor": next_cursor(), "createdAt": now_iso(), "event": event, "data": data})
