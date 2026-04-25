from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from ._utils import maybe_await, now_iso
from .storage import (
    ensureBaseLayout,
    listTaskRecords,
    listSessionMonitors,
    listSessionTaskRecords,
    loadHealthCheckTask,
    loadTaskRecord,
    readTaskMeta,
    saveHealthCheckTask,
    saveTaskRecord,
    taskOutputPath,
    writeTaskMeta,
)


_PROCESS_REGISTRY: dict[str, subprocess.Popen[Any]] = {}
_WATCHERS: set[threading.Thread | asyncio.Task[Any]] = set()


async def createTaskSupervisor(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd = args.get("cwd") or os.getcwd()
    on_event: Callable[[dict[str, Any]], Any] = args.get("onEvent") or (lambda _event: None)
    await ensureBaseLayout(cwd)
    supervisor_id = str(uuid.uuid4())
    subscribers: set[Callable[[dict[str, Any]], Any]] = set()
    attached_all_count = 0
    attached_sessions: dict[str, int] = {}

    async def emit(event: str, data: dict[str, Any], session_id: str | None = None) -> None:
        payload = {"event": event, "data": data, "sessionId": session_id}
        for subscriber in list(subscribers):
            await maybe_await(subscriber(payload))
        await maybe_await(on_event(payload))

    async def start_background_command(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        session_id = _require_string(task_args.get("sessionId") or task_args.get("session_id"), "sessionId")
        command = _require_string(task_args.get("command"), "command")
        task_cwd = os.path.abspath(str(task_args.get("cwd") or cwd))
        title = task_args.get("title")
        task_id = str(uuid.uuid4())
        output_file = taskOutputPath(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        started_at = now_iso()
        proc = _spawn_shell(command, task_cwd, output_file)
        _PROCESS_REGISTRY[task_id] = proc
        meta = {
            "type": "background_command",
            "taskId": task_id,
            "id": task_id,
            "sessionId": session_id,
            "command": command,
            "cwd": task_cwd,
            "startedAt": started_at,
            "updatedAt": started_at,
            "status": "running",
            "outputFile": output_file,
            "title": title,
            "description": title,
            "pid": proc.pid,
            "detailStatus": "running",
            "owner": "daemon",
            "supervisorId": supervisor_id,
        }
        await writeTaskMeta(task_id, meta)
        await saveTaskRecord(_build_task_record(meta, "Background command is running."))
        await emit("task.updated", _task_event_payload(meta), session_id)
        watcher = threading.Thread(target=_watch_process_thread, args=(task_id, command, task_cwd, output_file, emit), daemon=True)
        _WATCHERS.add(watcher)
        watcher.start()
        return {"taskId": task_id, "task_id": task_id, "outputFile": output_file, "output_file": output_file}

    async def start_subagent_task(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        parent_session_id = _require_string(task_args.get("parentSessionId") or task_args.get("parent_session_id"), "parentSessionId")
        session_id = _require_string(task_args.get("sessionId") or task_args.get("session_id"), "sessionId")
        agent_id = _require_string(task_args.get("agentId") or task_args.get("agent_id"), "agentId")
        task_cwd = os.path.abspath(str(task_args.get("cwd") or cwd))
        title = task_args.get("label") or task_args.get("title") or agent_id
        task_id = str(uuid.uuid4())
        output_file = taskOutputPath(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        started_at = now_iso()
        command = "printf 'Background subagent execution requires the product CLI runner.\\n'"
        proc = _spawn_shell(command, task_cwd, output_file)
        _PROCESS_REGISTRY[task_id] = proc
        meta = {
            "type": "subagent",
            "taskId": task_id,
            "id": task_id,
            "agent_id": agent_id,
            "sessionId": session_id,
            "session_id": session_id,
            "parent_session_id": parent_session_id,
            "parent_task_id": task_args.get("parentTaskId") or task_args.get("parent_task_id"),
            "command": command,
            "cwd": task_cwd,
            "pid": proc.pid,
            "startedAt": started_at,
            "updatedAt": started_at,
            "status": "running",
            "outputFile": output_file,
            "title": title,
            "description": title,
            "attachableSessionId": session_id,
            "retryable": False,
            "stoppable": True,
            "detailStatus": "worker_running",
            "owner": "daemon",
            "supervisorId": supervisor_id,
        }
        await writeTaskMeta(task_id, meta)
        record = _build_task_record(meta, "Background subagent is running under daemon supervision.")
        await saveTaskRecord(record)
        await emit("task.updated", _task_record_event_payload(record), parent_session_id)
        watcher = threading.Thread(target=_watch_process_thread, args=(task_id, command, task_cwd, output_file, emit), daemon=True)
        _WATCHERS.add(watcher)
        watcher.start()
        return {"taskId": task_id, "task_id": task_id, "outputFile": output_file, "output_file": output_file}

    async def read_task_output(task_id: str, tailBytes: int | None = None) -> dict[str, Any]:
        meta = await readTaskMeta(task_id)
        output_file = (meta or {}).get("outputFile") or taskOutputPath(task_id)
        path = Path(output_file)
        if not path.exists():
            return {"taskId": task_id, "output": "", "truncated": False, "outputFile": output_file}
        data = path.read_bytes()
        truncated = False
        if tailBytes and tailBytes > 0 and len(data) > tailBytes:
            data = data[-tailBytes:]
            truncated = True
        return {
            "taskId": task_id,
            "output": data.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "outputFile": output_file,
        }

    async def stop_task(task_id: str) -> bool:
        proc = _PROCESS_REGISTRY.get(task_id)
        meta = await readTaskMeta(task_id)
        stopped = False
        if meta:
            now = now_iso()
            meta.update({"status": "stopped", "updatedAt": now, "stoppedAt": now, "detailStatus": "stopped"})
            await writeTaskMeta(task_id, meta)
            await saveTaskRecord(_build_task_record(meta, "Task stopped by request."))
        if proc and proc.poll() is None:
            stopped = _terminate_process(proc.pid)
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
        elif meta and isinstance(meta.get("pid"), int):
            stopped = _terminate_process(meta["pid"])
        health = await loadHealthCheckTask(task_id)
        if health:
            stopped = True
            now = now_iso()
            health.update({"status": "stopped", "updatedAt": now, "stoppedAt": now})
            await saveHealthCheckTask(health)
        if meta:
            await emit("task.failed", _task_event_payload(meta), meta.get("sessionId"))
        _PROCESS_REGISTRY.pop(task_id, None)
        return stopped

    async def inspect_task(task_id: str) -> dict[str, Any] | None:
        record = await loadTaskRecord(task_id)
        if record:
            return record
        meta = await readTaskMeta(task_id)
        return _build_task_record(meta, None) if meta else None

    async def list_tasks(session_id: str | None = None) -> list[dict[str, Any]]:
        from .storage import listTaskRecords

        records = await listTaskRecords()
        if session_id:
            records = [record for record in records if record.get("sessionId") == session_id]
        return records

    async def retry_task(task_id: str) -> dict[str, Any]:
        meta = await readTaskMeta(task_id)
        if not meta or not meta.get("command"):
            raise RuntimeError(f"Task is not retryable: {task_id}")
        return await start_background_command(
            {
                "sessionId": meta.get("sessionId"),
                "command": meta.get("command"),
                "cwd": meta.get("cwd") or cwd,
                "title": meta.get("title"),
            }
        )

    async def start_health_check(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        task_id = str(uuid.uuid4())
        now = now_iso()
        record = {
            "type": "health_check",
            "taskId": task_id,
            "id": task_id,
            "sessionId": task_args.get("sessionId") or task_args.get("session_id"),
            "cwd": os.path.abspath(str(task_args.get("cwd") or cwd)),
            "title": _require_string(task_args.get("title"), "title"),
            "mode": task_args.get("mode") or ("command" if task_args.get("command") else "http"),
            "command": task_args.get("command"),
            "url": task_args.get("url"),
            "intervalMs": int(task_args.get("intervalMs") or task_args.get("interval_ms") or 30000),
            "timeoutMs": int(task_args.get("timeoutMs") or task_args.get("timeout_ms") or 5000),
            "status": "running",
            "startedAt": now,
            "updatedAt": now,
            "lastRunAt": None,
            "lastResult": None,
        }
        await saveHealthCheckTask(record)
        await writeTaskMeta(task_id, record)
        await saveTaskRecord(_build_task_record(record, "Health check is running."))
        watcher = asyncio.create_task(_run_health_check(record, emit))
        _WATCHERS.add(watcher)
        watcher.add_done_callback(_WATCHERS.discard)
        return {"taskId": task_id, "task_id": task_id}

    def subscribe(listener: Callable[[dict[str, Any]], Any], sessionId: str | dict[str, Any] | None = None):
        session_id = _subscription_session_id(sessionId)

        async def filtered(payload: dict[str, Any]) -> None:
            if _event_matches_session(payload, session_id):
                await maybe_await(listener(payload))

        subscribers.add(filtered)

        def unsubscribe() -> None:
            subscribers.discard(filtered)

        return unsubscribe

    def add_attachment(sessionId: str | dict[str, Any] | None = None):
        nonlocal attached_all_count
        session_id = _subscription_session_id(sessionId)
        if session_id:
            attached_sessions[session_id] = attached_sessions.get(session_id, 0) + 1
        else:
            attached_all_count += 1

        def release() -> None:
            nonlocal attached_all_count
            if session_id:
                next_count = attached_sessions.get(session_id, 1) - 1
                if next_count <= 0:
                    attached_sessions.pop(session_id, None)
                else:
                    attached_sessions[session_id] = next_count
            else:
                attached_all_count = max(0, attached_all_count - 1)

        return release

    async def close() -> None:
        for proc in list(_PROCESS_REGISTRY.values()):
            if proc.poll() is None:
                _terminate_process(proc.pid)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
        for watcher in list(_WATCHERS):
            if isinstance(watcher, threading.Thread) and watcher is not threading.current_thread() and watcher.is_alive():
                watcher.join(timeout=2)
            elif isinstance(watcher, asyncio.Task):
                watcher.cancel()
        await asyncio.gather(*(watcher for watcher in list(_WATCHERS) if isinstance(watcher, asyncio.Task)), return_exceptions=True)
        _PROCESS_REGISTRY.clear()

    def get_state() -> dict[str, Any]:
        return {
            "supervisorId": supervisor_id,
            "watchedSessions": len(attached_sessions),
            "activeTasks": sum(1 for proc in _PROCESS_REGISTRY.values() if proc.poll() is None),
            "attachedClients": attached_all_count + sum(attached_sessions.values()),
        }

    async def get_bootstrap(session_id: str) -> dict[str, Any]:
        return {
            "taskSnapshot": _task_snapshot(await listSessionTaskRecords(session_id)),
            "monitorSnapshot": _monitor_snapshot(await listSessionMonitors(session_id)),
            "collaborationSnapshot": _empty_collaboration_snapshot(),
        }

    await _reconcile_persisted_tasks(supervisor_id, emit)

    return {
        "close": close,
        "addAttachment": add_attachment,
        "getBootstrap": get_bootstrap,
        "listTasks": list_tasks,
        "inspectTask": inspect_task,
        "readTaskOutput": read_task_output,
        "stopTask": stop_task,
        "retryTask": retry_task,
        "startBackgroundCommand": start_background_command,
        "startMainSessionTask": lambda task_args=None, **task_kwargs: start_background_command(
            {
                **(task_args or {}),
                **task_kwargs,
                "command": (task_args or {}).get("prompt") or task_kwargs.get("prompt") or "",
                "title": (task_args or {}).get("title") or task_kwargs.get("title") or "Detached main-session task",
            }
        ),
        "startSubagentTask": start_subagent_task,
        "startHealthCheck": start_health_check,
        "subscribe": subscribe,
        "getState": get_state,
    }


def _spawn_shell(command: str, cwd: str, output_file: str) -> subprocess.Popen[Any]:
    handle = open(output_file, "ab")
    kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid
    try:
        return subprocess.Popen(
            command,
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            shell=True,
            **kwargs,
        )
    finally:
        handle.close()


def _watch_process_thread(
    task_id: str,
    command: str,
    cwd: str,
    output_file: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> None:
    proc = _PROCESS_REGISTRY.get(task_id)
    if not proc:
        return
    code = proc.wait()
    _PROCESS_REGISTRY.pop(task_id, None)
    now = now_iso()
    previous = asyncio.run(readTaskMeta(task_id)) or {}
    status = "completed" if code == 0 and previous.get("status") != "stopped" else "failed"
    if previous.get("status") == "stopped":
        status = "stopped"
    meta = {
        **previous,
        "type": previous.get("type") or "background_command",
        "taskId": task_id,
        "id": task_id,
        "command": previous.get("command") or command,
        "cwd": previous.get("cwd") or cwd,
        "outputFile": previous.get("outputFile") or output_file,
        "status": status,
        "exitCode": code,
        "finishedAt": now,
        "updatedAt": now,
        "detailStatus": status,
    }
    asyncio.run(writeTaskMeta(task_id, meta))
    asyncio.run(saveTaskRecord(_build_task_record(meta, f"Command exited with code {code}.")))
    event_session_id = meta.get("parent_session_id") if meta.get("type") == "subagent" else meta.get("sessionId")
    asyncio.run(maybe_await(emit("task.completed" if status == "completed" else "task.failed", _task_event_payload(meta), event_session_id)))
    _WATCHERS.discard(threading.current_thread())


async def _run_health_check(record: dict[str, Any], emit: Callable[[str, dict[str, Any], str | None], Any]) -> None:
    task_id = record["taskId"]
    while True:
        current = await loadHealthCheckTask(task_id)
        if not current or current.get("status") != "running":
            return
        result = await _execute_health_check(current)
        now = now_iso()
        current.update({"lastRunAt": now, "lastResult": result, "updatedAt": now, "status": "running" if result.get("ok") else "failed"})
        await saveHealthCheckTask(current)
        await writeTaskMeta(task_id, current)
        await saveTaskRecord(_build_task_record(current, result.get("summary")))
        await maybe_await(emit("task.updated" if result.get("ok") else "task.failed", _task_event_payload(current), current.get("sessionId")))
        if not result.get("ok"):
            return
        await asyncio.sleep(max(1, int(current.get("intervalMs") or 30000) / 1000))


async def _execute_health_check(record: dict[str, Any]) -> dict[str, Any]:
    timeout = max(1, int(record.get("timeoutMs") or 5000) / 1000)
    if record.get("mode") == "command":
        command = record.get("command")
        if not command:
            return {"ok": False, "summary": "No health-check command configured."}
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=record.get("cwd") or os.getcwd(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "summary": "Health check timed out."}
        output = (stdout + stderr).decode("utf-8", errors="replace").strip()
        return {"ok": proc.returncode == 0, "summary": output[-500:] if output else f"exit {proc.returncode}", "exitCode": proc.returncode}
    import urllib.request

    url = record.get("url")
    if not url:
        return {"ok": False, "summary": "No health-check URL configured."}

    def fetch() -> dict[str, Any]:
        with urllib.request.urlopen(str(url), timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return {"status": response.status, "body": body}

    try:
        response = await asyncio.to_thread(fetch)
    except Exception as exc:
        return {"ok": False, "summary": str(exc)}
    expected = record.get("expectedStatusCodes") or [200]
    ok = response["status"] in expected
    expected_body = record.get("expectedBodySubstring")
    if expected_body:
        ok = ok and expected_body in response["body"]
    return {"ok": ok, "summary": f"HTTP {response['status']}", "status": response["status"]}


async def _reconcile_persisted_tasks(
    supervisor_id: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> None:
    records = await listTaskRecords()
    active_statuses = {"backgrounded", "running", "reconciling", "degraded"}
    recoverable_kinds = {"background_command", "main_session", "subagent"}
    for record in records:
        if record.get("owner") != "daemon":
            continue
        if record.get("kind") not in recoverable_kinds or record.get("status") not in active_statuses:
            continue
        task_id = record.get("id") or record.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            continue
        meta = await readTaskMeta(task_id)
        if not meta:
            continue
        pid = meta.get("pid")
        if not isinstance(pid, int):
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            pid = metadata.get("pid")
        if isinstance(pid, int) and _is_process_alive(pid):
            await _mark_recovered_process(task_id, pid, record, meta, supervisor_id, emit)
        else:
            await _mark_lost_process(task_id, record, meta, supervisor_id, emit)


async def _mark_recovered_process(
    task_id: str,
    pid: int,
    record: dict[str, Any],
    meta: dict[str, Any],
    supervisor_id: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> None:
    now = now_iso()
    metadata = dict(record.get("metadata") or {})
    metadata["recovery_state"] = "recovered_after_daemon_restart"
    meta_metadata = dict(meta.get("metadata") or {})
    meta_metadata["recovery_state"] = "recovered_after_daemon_restart"
    next_meta = {
        **meta,
        "status": "reconciling",
        "detailStatus": "recovered_after_daemon_restart",
        "updatedAt": now,
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "metadata": meta_metadata,
    }
    summary = (
        "Recovered after daemon restart. The worker is still alive, but supervisor tracking is degraded until it exits."
        if record.get("kind") == "subagent"
        else "Recovered after daemon restart. The process is still alive, but lifecycle tracking is degraded until it exits."
    )
    next_record = {
        **_build_task_record(next_meta, summary),
        **record,
        "status": "degraded",
        "summary": summary,
        "updatedAt": now,
        "detailStatus": "recovered_after_daemon_restart",
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "metadata": metadata,
    }
    await writeTaskMeta(task_id, next_meta)
    await saveTaskRecord(next_record)
    await maybe_await(emit("task.updated", _task_record_event_payload(next_record), record.get("sessionId")))
    watcher = asyncio.create_task(_watch_recovered_process(task_id, pid, record.get("sessionId"), emit))
    _WATCHERS.add(watcher)
    watcher.add_done_callback(_WATCHERS.discard)


async def _mark_lost_process(
    task_id: str,
    record: dict[str, Any],
    meta: dict[str, Any],
    supervisor_id: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> None:
    now = now_iso()
    summary = (
        "Daemon restarted while this worker was active and the worker process is no longer running."
        if record.get("kind") == "subagent"
        else "Daemon restarted while this task was active and the process is no longer running."
    )
    next_meta = {
        **meta,
        "status": "failed",
        "detailStatus": "failed",
        "error": summary,
        "updatedAt": now,
        "finishedAt": now,
        "owner": "daemon",
        "supervisorId": supervisor_id,
    }
    next_record = {
        **_build_task_record(next_meta, summary),
        **record,
        "status": "failed",
        "summary": summary,
        "updatedAt": now,
        "completedAt": now,
        "detailStatus": "failed",
        "owner": "daemon",
        "supervisorId": supervisor_id,
    }
    await writeTaskMeta(task_id, next_meta)
    await saveTaskRecord(next_record)
    await maybe_await(emit("task.failed", _task_record_event_payload(next_record), record.get("sessionId")))


async def _watch_recovered_process(
    task_id: str,
    pid: int,
    session_id: str | None,
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> None:
    while _is_process_alive(pid):
        await asyncio.sleep(1)
    existing = await loadTaskRecord(task_id)
    now = now_iso()
    summary = "Recovered daemon-owned task exited after restart. Final completion details were unavailable."
    next_record = {
        **(existing or {"id": task_id, "sessionId": session_id, "kind": "background_command"}),
        "status": "stopped",
        "summary": summary,
        "updatedAt": now,
        "completedAt": now,
    }
    await saveTaskRecord(next_record)
    meta = await readTaskMeta(task_id) or {}
    await writeTaskMeta(task_id, {**meta, "status": "terminated", "updatedAt": now, "finishedAt": now})
    await maybe_await(emit("task.updated", _task_record_event_payload(next_record), session_id))


def _build_task_record(meta: dict[str, Any], summary: str | None) -> dict[str, Any]:
    now = now_iso()
    task_id = meta.get("taskId") or meta.get("id")
    output_file = meta.get("outputFile") or taskOutputPath(task_id)
    output_cursor = Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0
    status = _map_status(meta.get("status"))
    is_subagent = meta.get("type") == "subagent"
    metadata = dict(meta.get("metadata") or {})
    metadata.update({"pid": meta.get("pid"), "exitCode": meta.get("exitCode"), "lastResult": meta.get("lastResult")})
    if is_subagent:
        metadata.update(
            {
                "agent_id": meta.get("agent_id"),
                "child_session_id": meta.get("session_id") or meta.get("sessionId"),
                "parent_session_id": meta.get("parent_session_id"),
                "parent_task_id": meta.get("parent_task_id"),
            }
        )
    return {
        "id": task_id,
        "sessionId": meta.get("parent_session_id") if is_subagent else meta.get("sessionId"),
        "sourceId": meta.get("agent_id") if is_subagent else task_id,
        "kind": "health_check" if meta.get("type") == "health_check" else meta.get("type") or "background_command",
        "status": status,
        "title": meta.get("title") or meta.get("description") or _summarize_command(meta.get("command")),
        "summary": summary or meta.get("error") or meta.get("detailStatus") or status,
        "createdAt": meta.get("startedAt") or now,
        "updatedAt": meta.get("updatedAt") or meta.get("finishedAt") or now,
        "startedAt": meta.get("startedAt"),
        "completedAt": meta.get("finishedAt") or meta.get("stoppedAt"),
        "cwd": meta.get("cwd"),
        "command": meta.get("command"),
        "outputFile": output_file,
        "outputCursor": output_cursor,
        "foregroundMode": "background",
        "taskFamily": "health_check" if meta.get("type") == "health_check" else "subagent" if is_subagent else "shell",
        "detailStatus": meta.get("detailStatus") or status,
        "retrieveOnly": False if is_subagent else True,
        "attachableSessionId": meta.get("attachableSessionId") if is_subagent else None,
        "parentTaskId": meta.get("parent_task_id") if is_subagent else None,
        "relatedChildSessionIds": [meta.get("session_id") or meta.get("sessionId")] if is_subagent and (meta.get("session_id") or meta.get("sessionId")) else [],
        "owner": meta.get("owner") or "daemon",
        "supervisorId": meta.get("supervisorId"),
        "metadata": metadata,
    }


def _task_snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = records[:16]
    return {
        "total": len(records),
        "active": sum(1 for record in records if _is_active_task_status(record.get("status"))),
        "backgrounded": sum(1 for record in records if record.get("status") == "backgrounded"),
        "awaitingInput": sum(1 for record in records if record.get("status") == "awaiting_input"),
        "failed": sum(1 for record in records if record.get("status") == "failed"),
        "labels": [record.get("title") for record in tasks if _is_active_task_status(record.get("status")) and record.get("title")][:6],
        "tasks": tasks,
    }


def _monitor_snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    active = [record for record in records if record.get("status") == "active"]
    next_check_in_ms = None
    if active:
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000
        candidates = []
        for record in active:
            last_check = _parse_iso_ms(record.get("lastCheckAt") or record.get("lastRunAt")) or 0
            candidates.append(max(0, last_check + int(record.get("checkIntervalMs") or 5000) - now))
        next_check_in_ms = _bucket_ms(min(candidates))
    return {
        "total": len(records),
        "active": len(active),
        "failed": sum(1 for record in records if record.get("status") == "failed"),
        "stopped": sum(1 for record in records if record.get("status") == "stopped"),
        "muted": sum(1 for record in records if record.get("status") == "muted"),
        "nextCheckInMs": next_check_in_ms,
        "labels": [_summarize_goal(record.get("goal", "")) for record in active[:3]],
    }


def _empty_collaboration_snapshot() -> dict[str, Any]:
    return {
        "workers": [],
        "activeCount": 0,
        "awaitingInputCount": 0,
        "backgroundCount": 0,
        "labels": [],
    }


def _is_active_task_status(status: Any) -> bool:
    return status in {"running", "backgrounded", "awaiting_input", "reconciling", "degraded"}


def _parse_iso_ms(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    import datetime

    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def _bucket_ms(value: float) -> int:
    import math

    if value <= 5000:
        return int(math.ceil(value / 1000) * 1000)
    return int(math.ceil(value / 5000) * 5000)


def _summarize_goal(goal: Any) -> str:
    text = str(goal or "").strip()
    return text if len(text) <= 48 else f"{text[:45].rstrip()}..."


def _task_event_payload(meta: dict[str, Any]) -> dict[str, Any]:
    record = _build_task_record(meta, None)
    return _task_record_event_payload(record)


def _task_record_event_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": record.get("sessionId"),
        "task_id": record.get("id"),
        "kind": record.get("kind"),
        "status": record.get("status"),
        "title": record.get("title"),
        "summary": record.get("summary"),
        "updated_at": record.get("updatedAt"),
    }


def _map_status(value: Any) -> str:
    if value in ("running", "completed", "failed", "stopped", "queued", "awaiting_input", "reconciling", "degraded"):
        return str(value)
    if value in ("terminated", "cancelled"):
        return "stopped"
    return "running"


def _subscription_session_id(value: str | dict[str, Any] | None) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        maybe = value.get("sessionId") or value.get("session_id")
        return maybe if isinstance(maybe, str) and maybe else None
    return None


def _event_matches_session(payload: dict[str, Any], session_id: str | None) -> bool:
    if not session_id:
        return True
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("sessionId") == session_id or data.get("sessionId") == session_id or data.get("session_id") == session_id


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_process(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        with contextlib.suppress(Exception):
            os.kill(pid, signal.SIGTERM)
            return True
        return False


def _summarize_command(command: Any) -> str:
    text = str(command or "task").strip()
    return text if len(text) <= 80 else f"{text[:77]}..."


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected {field} to be a non-empty string")
    return value
