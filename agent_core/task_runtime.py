from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from .storage import listSessionTaskRecords, readTaskMeta, saveTaskRecord, taskOutputPath


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
    snapshot = _empty_snapshot()
    previous: dict[str, dict[str, Any]] = {}

    async def tick() -> None:
        nonlocal snapshot, previous
        if closed or not session_ref.get("id"):
            return
        records = await _project_session_tasks(session_ref)
        for record in records:
            await saveTaskRecord(record)
        next_map = {record["id"]: record for record in records}
        for record in records:
            old = previous.get(record["id"])
            if not old:
                emit("task.registered", _event_payload(record))
            elif old.get("status") != record.get("status") or old.get("outputCursor") != record.get("outputCursor"):
                event = "task.completed" if record.get("status") == "completed" else "task.failed" if record.get("status") in ("failed", "stopped") else "task.updated"
                emit(event, _event_payload(record))
        previous = next_map
        snapshot = _summarize(records)
        emit("task.snapshot", {"session_id": session_ref.get("id"), **snapshot})

    async def run_loop() -> None:
        while not closed:
            await tick()
            await asyncio.sleep(tick_interval)

    loop_task = asyncio.create_task(run_loop())

    async def close() -> None:
        nonlocal closed
        closed = True
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    def set_session(session: dict[str, Any], config: dict[str, Any]) -> None:
        nonlocal session_ref, config_ref, previous
        session_ref = session
        config_ref = config
        previous = {}

    def set_foreground_busy(busy: bool) -> None:
        nonlocal foreground_busy
        foreground_busy = busy

    await tick()
    return {
        "close": close,
        "setSession": set_session,
        "setForegroundBusy": set_foreground_busy,
        "getSnapshot": lambda: {**snapshot, "foregroundBusy": foreground_busy},
    }


async def _project_session_tasks(session: dict[str, Any]) -> list[dict[str, Any]]:
    records = await listSessionTaskRecords(session["id"])
    seen = {record["id"] for record in records}
    for task_id in session.get("metadata", {}).get("taskIds", []) or []:
        if task_id in seen:
            continue
        meta = await readTaskMeta(task_id)
        if meta:
            records.append(_record_from_meta(meta))
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)


def _record_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    task_id = meta.get("taskId") or meta.get("id")
    output_file = meta.get("outputFile") or taskOutputPath(task_id)
    output_cursor = Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0
    status = meta.get("status") if meta.get("status") in ("running", "completed", "failed", "stopped") else "running"
    return {
        "id": task_id,
        "sessionId": meta.get("sessionId"),
        "kind": meta.get("type") or "background_command",
        "status": status,
        "title": meta.get("title") or meta.get("description") or meta.get("command") or task_id,
        "summary": meta.get("detailStatus") or status,
        "createdAt": meta.get("startedAt"),
        "updatedAt": meta.get("updatedAt") or meta.get("finishedAt") or meta.get("startedAt"),
        "cwd": meta.get("cwd"),
        "command": meta.get("command"),
        "outputFile": output_file,
        "outputCursor": output_cursor,
        "foregroundMode": "background",
        "taskFamily": "health_check" if meta.get("type") == "health_check" else "shell",
        "detailStatus": meta.get("detailStatus") or status,
        "retrieveOnly": True,
    }


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    active = [record for record in records if record.get("status") in ("running", "queued", "awaiting_input")]
    failed = [record for record in records if record.get("status") in ("failed", "stopped")]
    awaiting = [record for record in records if record.get("status") == "awaiting_input"]
    labels = [str(record.get("title")) for record in records if record.get("title")]
    return {
        "total": len(records),
        "active": len(active),
        "backgrounded": len(active),
        "awaitingInput": len(awaiting),
        "failed": len(failed),
        "labels": labels,
        "tasks": records,
    }


def _event_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": record.get("sessionId"),
        "task_id": record.get("id"),
        "kind": record.get("kind"),
        "status": record.get("status"),
        "title": record.get("title"),
        "summary": record.get("summary"),
        "updated_at": record.get("updatedAt"),
    }
