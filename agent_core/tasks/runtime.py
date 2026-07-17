from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from .supervisor import _is_active_task_status, _project_session_task_records, _task_record_event_payload
from ..runtime_identifiers import tryParseTaskId
from ..storage import saveTaskRecord


def _empty_snapshot() -> dict[str, Any]:
    return {"total": 0, "active": 0, "backgrounded": 0, "awaitingInput": 0, "failed": 0, "labels": [], "tasks": []}


async def createTaskRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session_ref = args.get("session") or {}
    config_ref = args.get("config") or {}
    emit: Callable[[str, dict[str, Any]], Any] = args.get("emit") or (lambda *_: None)
    tick_interval = int(args.get("tickIntervalMs") or 1500) / 1000
    closed = False
    foreground_busy = False
    tick_in_flight = False
    snapshot = _empty_snapshot()
    previous: dict[str, dict[str, Any]] = {}
    restored_for_session_id: str | None = None
    stop_event = threading.Event()
    tick_thread: threading.Thread | None = None

    async def tick() -> None:
        nonlocal snapshot, previous, tick_in_flight, restored_for_session_id
        if closed or tick_in_flight or not session_ref.get("id"):
            return
        tick_in_flight = True
        try:
            records = await _project_session_task_records(session_ref["id"], session_ref)
            for record in records:
                # Monitor/remote rows use virtual display references. They are
                # projected from their owning stores and must never become
                # task-record filenames.
                if tryParseTaskId(record.get("id")) is not None:
                    await saveTaskRecord(record)
            next_map = {record["id"]: record for record in records if record.get("id")}

            if restored_for_session_id != session_ref["id"]:
                restored_for_session_id = session_ref["id"]
                for record in records:
                    if _is_active_task_status(record.get("status")):
                        emit("task.restored", _task_record_event_payload(record))

            for record in records:
                old = previous.get(record.get("id"))
                if not old:
                    emit("task.registered", _task_record_event_payload(record))
                    continue
                if isinstance(record.get("outputCursor"), int) and isinstance(old.get("outputCursor"), int) and record.get("outputCursor") != old.get("outputCursor"):
                    emit(
                        "task.output_delta",
                        {
                            "session_id": record.get("sessionId"),
                            "task_id": record.get("id"),
                            "bytes_delta": max(0, int(record.get("outputCursor") or 0) - int(old.get("outputCursor") or 0)),
                            "output_cursor": record.get("outputCursor"),
                        },
                    )
                status_changed = record.get("status") != old.get("status")
                detail_changed = record.get("summary") != old.get("summary") or record.get("title") != old.get("title")
                if status_changed:
                    event = (
                        "task.completed"
                        if record.get("status") == "completed"
                        else "task.failed"
                        if record.get("status") == "failed"
                        else "task.stopped"
                        if record.get("status") == "stopped"
                        else "task.updated"
                    )
                    emit(event, _task_record_event_payload(record))
                elif detail_changed and record.get("status") not in {"completed", "failed", "stopped"}:
                    emit("task.updated", _task_record_event_payload(record))

            previous = next_map
            next_snapshot = _summarize(records)
            if next_snapshot != snapshot:
                snapshot = next_snapshot
                emit("task.snapshot", _snapshot_payload(session_ref.get("id"), snapshot))
        finally:
            tick_in_flight = False

    async def close() -> None:
        nonlocal closed
        closed = True
        stop_event.set()
        if tick_thread and tick_thread.is_alive() and threading.current_thread() is not tick_thread:
            tick_thread.join(timeout=2)

    def set_session(session: dict[str, Any], config: dict[str, Any]) -> None:
        nonlocal session_ref, config_ref, previous, restored_for_session_id
        session_ref = session
        config_ref = config
        previous = {}
        restored_for_session_id = None
        _schedule_tick()

    def set_foreground_busy(busy: bool) -> None:
        nonlocal foreground_busy
        foreground_busy = busy
        if not busy:
            _schedule_tick()

    def _schedule_tick() -> None:
        if not closed:
            threading.Thread(target=_run_tick_once, name="agent-core-task-runtime-refresh", daemon=True).start()

    def _run_tick_once() -> None:
        try:
            asyncio.run(tick())
        except Exception as exc:
            emit("task.tick_failed", {"session_id": session_ref.get("id"), "error": str(exc)})

    def _run_loop() -> None:
        while not stop_event.wait(tick_interval):
            _run_tick_once()

    await tick()
    if args.get("startTicker", True):
        tick_thread = threading.Thread(target=_run_loop, name=f"agent-core-task-runtime-{str(session_ref.get('id') or '')[:8]}", daemon=True)
        tick_thread.start()
    return {
        "close": close,
        "setSession": set_session,
        "setForegroundBusy": set_foreground_busy,
        "getSnapshot": lambda: snapshot,
    }


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = records[:16]
    active = [record for record in records if _is_active_task_status(record.get("status"))]
    failed = [record for record in records if record.get("status") == "failed"]
    awaiting = [record for record in records if record.get("status") == "awaiting_input"]
    labels = [str(record.get("title")) for record in tasks if _is_active_task_status(record.get("status")) and record.get("title")][:6]
    return {
        "total": len(records),
        "active": len(active),
        "backgrounded": sum(1 for record in records if record.get("status") == "backgrounded"),
        "awaitingInput": len(awaiting),
        "failed": len(failed),
        "labels": labels,
        "tasks": tasks,
    }


def _snapshot_payload(session_id: str | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "total": snapshot.get("total", 0),
        "active": snapshot.get("active", 0),
        "backgrounded": snapshot.get("backgrounded", 0),
        "awaiting_input": snapshot.get("awaitingInput", 0),
        "failed": snapshot.get("failed", 0),
        "labels": snapshot.get("labels", []),
        "tasks": [
            {
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
            for task in snapshot.get("tasks", [])
        ],
    }
