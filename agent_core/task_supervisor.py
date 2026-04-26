from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
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
    loadChildAgent,
    loadHealthCheckTask,
    loadSession,
    loadTaskRecord,
    readTaskMeta,
    saveHealthCheckTask,
    saveChildAgent,
    saveSession,
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
        session = await loadSession(session_id)
        if session:
            metadata = session.setdefault("metadata", {})
            metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
            await saveSession(session)
        await emit("task.updated", _task_event_payload(meta), session_id)
        watcher = threading.Thread(target=_watch_process_thread, args=(task_id, command, task_cwd, output_file, emit), daemon=True)
        _WATCHERS.add(watcher)
        watcher.start()
        return {"taskId": task_id, "task_id": task_id, "outputFile": output_file, "output_file": output_file}

    async def start_main_session_task(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        session_id = _require_string(task_args.get("sessionId") or task_args.get("session_id"), "sessionId")
        prompt = _require_string(task_args.get("prompt"), "prompt")
        task_cwd = os.path.abspath(str(task_args.get("cwd") or cwd))
        title = task_args.get("title") or "Detached main-session task"
        session = await loadSession(session_id)
        if not session:
            raise RuntimeError(f"Session not found: {session_id}")
        task_id = str(uuid.uuid4())
        output_file = taskOutputPath(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        started_at = now_iso()
        metadata = session.setdefault("metadata", {})
        metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
        metadata["backgroundSessionIds"] = _dedupe([*(metadata.get("backgroundSessionIds") or []), session_id])
        await saveSession(session)
        command = prompt
        proc = _spawn_main_session_worker(task_id, session_id, prompt, task_cwd, output_file)
        _PROCESS_REGISTRY[task_id] = proc
        meta = {
            "type": "main_session",
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
            "description": prompt[:180],
            "pid": proc.pid,
            "attachableSessionId": session_id,
            "detailStatus": "session_starting",
            "owner": "daemon",
            "supervisorId": supervisor_id,
        }
        await writeTaskMeta(task_id, meta)
        record = _build_task_record(meta, "Detached main-session work is running in the daemon.")
        await saveTaskRecord(record)
        await emit("task.updated", _task_record_event_payload(record), session_id)
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
        command = f"{sys.executable} -m agent_core.background_subagent_runner --session-id {session_id} --agent-id {agent_id} --task-id {task_id}"
        proc = _spawn_subagent_worker(task_id, session_id, agent_id, task_cwd, output_file)
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
            if meta.get("type") == "subagent":
                meta.update({"completionReason": "cancelled", "resultSummary": "Stopped by request.", "retryable": True, "stoppable": False})
                await _mark_subagent_cancelled_from_stop(meta, task_id, now)
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
        record = await loadTaskRecord(task_id)
        meta = await readTaskMeta(task_id)
        if not record and (not meta or not meta.get("command")):
            raise RuntimeError(f"Task is not retryable: {task_id}")
        kind = (record or {}).get("kind") or (meta or {}).get("type")
        source = record or meta or {}
        if kind == "background_command":
            command = source.get("command")
            if not command:
                raise RuntimeError("Background command task is missing retry metadata.")
            return await start_background_command(
                {
                    "sessionId": source.get("sessionId"),
                    "command": command,
                    "cwd": source.get("cwd") or cwd,
                    "title": source.get("title"),
                }
            )
        if kind == "main_session":
            prompt = source.get("command")
            if not prompt:
                raise RuntimeError("Main-session task is missing retry metadata.")
            return await start_main_session_task(
                {
                    "sessionId": source.get("sessionId"),
                    "prompt": prompt,
                    "cwd": source.get("cwd") or cwd,
                    "title": source.get("title"),
                }
            )
        if kind == "health_check":
            health = await loadHealthCheckTask(task_id)
            if not health:
                raise RuntimeError(f"Health check metadata not found: {task_id}")
            return await start_health_check(health)
        if kind == "subagent":
            metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            agent_id = metadata.get("agent_id") or source.get("sourceId") or (meta or {}).get("agent_id")
            child = await loadChildAgent(agent_id) if isinstance(agent_id, str) and agent_id else None
            if not child:
                raise RuntimeError(f"Subagent metadata not found for task: {task_id}")
            child_session = await loadSession(child["sessionId"])
            if not child_session:
                raise RuntimeError(f"Subagent session not found: {child['sessionId']}")
            return await start_subagent_task(
                {
                    "parentSessionId": child.get("parentSessionId"),
                    "sessionId": child.get("sessionId"),
                    "agentId": child.get("agentId"),
                    "cwd": child_session.get("cwd") or cwd,
                    "label": child.get("name") or child.get("agentType") or child.get("agentId"),
                    "parentTaskId": child.get("parentTaskId"),
                }
            )
        raise RuntimeError(f"Retry is not supported for task kind: {kind}")

    async def start_health_check(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        session_id = task_args.get("sessionId") or task_args.get("session_id")
        task_id = str(uuid.uuid4())
        now = now_iso()
        interval_ms = max(250, int(task_args.get("intervalMs") or task_args.get("interval_ms") or 10000))
        timeout_ms = max(1000, int(task_args.get("timeoutMs") or task_args.get("timeout_ms") or 10000))
        max_interval_ms = max(250, int(task_args.get("maxIntervalMs") or task_args.get("max_interval_ms") or interval_ms * 4))
        record = {
            "type": "health_check",
            "taskId": task_id,
            "id": task_id,
            "sessionId": session_id,
            "cwd": os.path.abspath(str(task_args.get("cwd") or cwd)),
            "title": _require_string(task_args.get("title"), "title"),
            "mode": task_args.get("mode") or ("command" if task_args.get("command") else "http"),
            "command": task_args.get("command"),
            "url": task_args.get("url"),
            "intervalMs": interval_ms,
            "currentIntervalMs": interval_ms,
            "timeoutMs": timeout_ms,
            "backoffMultiplier": max(1, float(task_args.get("backoffMultiplier") or task_args.get("backoff_multiplier") or 2)),
            "maxIntervalMs": max(interval_ms, max_interval_ms),
            "failureThreshold": max(1, int(task_args.get("failureThreshold") or task_args.get("failure_threshold") or 2)),
            "expectedStatusCodes": task_args.get("expectedStatusCodes") or task_args.get("expected_status_codes"),
            "expectedBodySubstring": task_args.get("expectedBodySubstring") or task_args.get("expected_body_substring"),
            "status": "active",
            "createdAt": now,
            "startedAt": now,
            "updatedAt": now,
            "lastRunAt": None,
            "lastResult": None,
            "consecutiveFailures": 0,
            "outputFile": taskOutputPath(task_id),
        }
        await saveHealthCheckTask(record)
        await writeTaskMeta(task_id, record)
        await saveTaskRecord(_build_task_record(record, "Health check is running."))
        if isinstance(session_id, str) and session_id:
            session = await loadSession(session_id)
            if session:
                metadata = session.setdefault("metadata", {})
                metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
                await saveSession(session)
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
        "startMainSessionTask": start_main_session_task,
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


def _spawn_main_session_worker(task_id: str, session_id: str, prompt: str, cwd: str, output_file: str) -> subprocess.Popen[Any]:
    handle = open(output_file, "ab")
    kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid
    env = _worker_env()
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_core.background_main_session_runner",
                "--session-id",
                session_id,
                "--task-id",
                task_id,
                "--prompt",
                prompt,
            ],
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            **kwargs,
        )
    finally:
        handle.close()


def _spawn_subagent_worker(task_id: str, session_id: str, agent_id: str, cwd: str, output_file: str) -> subprocess.Popen[Any]:
    handle = open(output_file, "ab")
    kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid
    env = _worker_env(
        {
            "TESS_CHILD_AGENT_ID": agent_id,
            "TESS_CHILD_TASK_ID": task_id,
            "TESS_CHILD_SESSION_ID": session_id,
        }
    )
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_core.background_subagent_runner",
                "--session-id",
                session_id,
                "--agent-id",
                agent_id,
                "--task-id",
                task_id,
            ],
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            **kwargs,
        )
    finally:
        handle.close()


def _worker_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, **(extra or {})}
    package_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = package_root if not env.get("PYTHONPATH") else f"{package_root}{os.pathsep}{env['PYTHONPATH']}"
    return env


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
    elif code is not None and code < 0:
        status = "stopped" if abs(code) in {signal.SIGINT, signal.SIGTERM} else "failed"
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
    asyncio.run(saveTaskRecord(_build_task_record(meta, _process_exit_summary(status, code))))
    event_session_id = meta.get("parent_session_id") if meta.get("type") == "subagent" else meta.get("sessionId")
    asyncio.run(maybe_await(emit("task.completed" if status == "completed" else "task.failed", _task_event_payload(meta), event_session_id)))
    _WATCHERS.discard(threading.current_thread())


async def _run_health_check(record: dict[str, Any], emit: Callable[[str, dict[str, Any], str | None], Any]) -> None:
    task_id = record["taskId"]
    while True:
        current = await loadHealthCheckTask(task_id)
        if not current or current.get("status") not in {"active", "unhealthy", "running"}:
            return
        previous_status = current.get("status")
        result = await _execute_health_check(current)
        now = now_iso()
        failure_threshold = max(1, int(current.get("failureThreshold") or 2))
        consecutive_failures = 0 if result.get("ok") else int(current.get("consecutiveFailures") or 0) + 1
        next_status = "active" if result.get("ok") else "failed" if consecutive_failures >= failure_threshold else "unhealthy"
        current.update(
            {
                "lastRunAt": now,
                "lastResult": result,
                "updatedAt": now,
                "status": next_status,
                "consecutiveFailures": consecutive_failures,
                "currentIntervalMs": int(current.get("intervalMs") or 10000) if result.get("ok") else _next_backoff_interval_ms(current, consecutive_failures),
                "lastSuccessAt": now if result.get("ok") else current.get("lastSuccessAt"),
                "lastFailureAt": current.get("lastFailureAt") if result.get("ok") else now,
                "lastError": None if result.get("ok") else result.get("error") or result.get("summary"),
                "lastExitCode": result.get("exitCode", current.get("lastExitCode")),
                "lastStatusCode": result.get("status", current.get("lastStatusCode")),
                "lastResponseSnippet": result.get("snippet", current.get("lastResponseSnippet")),
            }
        )
        output_file = current.get("outputFile") or taskOutputPath(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"[{now}] {result.get('output') or result.get('summary') or ''}\n")
        current["outputFile"] = output_file
        await saveHealthCheckTask(current)
        await writeTaskMeta(task_id, current)
        await saveTaskRecord(_build_task_record(current, result.get("summary")))
        if next_status == "unhealthy":
            await maybe_await(
                emit(
                    "health_check.unhealthy",
                    {"task_id": task_id, "title": current.get("title"), "failures": consecutive_failures, "error": current.get("lastError")},
                    current.get("sessionId"),
                )
            )
        elif previous_status != "active" and next_status == "active":
            await maybe_await(emit("health_check.recovered", {"task_id": task_id, "title": current.get("title")}, current.get("sessionId")))
        await maybe_await(emit("task.failed" if next_status == "failed" else "task.updated", _task_event_payload(current), current.get("sessionId")))
        if next_status == "failed":
            return
        await asyncio.sleep(max(0.25, int(current.get("currentIntervalMs") or current.get("intervalMs") or 10000) / 1000))


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
            await proc.wait()
            return {"ok": False, "summary": "Health check timed out.", "output": "Health check timed out.", "error": "Health check timed out."}
        output = (stdout + stderr).decode("utf-8", errors="replace").strip()
        expected_body = record.get("expectedBodySubstring")
        body_ok = expected_body in output if expected_body else True
        ok = proc.returncode == 0 and body_ok
        error = None if ok else f"Health check exited with code {proc.returncode}" if proc.returncode != 0 else f"Command output did not contain expected text: {expected_body}"
        return {
            "ok": ok,
            "summary": output[-500:] if output else f"exit {proc.returncode}",
            "output": output,
            "snippet": output[:500],
            "exitCode": proc.returncode,
            "error": error,
        }
    import urllib.request
    import urllib.error

    url = record.get("url")
    if not url:
        return {"ok": False, "summary": "No health-check URL configured.", "error": "No health-check URL configured."}

    def fetch() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(str(url), timeout=timeout) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
                return {"status": response.status, "body": body}
        except urllib.error.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", errors="replace")
            return {"status": exc.code, "body": body}

    try:
        response = await asyncio.to_thread(fetch)
    except Exception as exc:
        return {"ok": False, "summary": str(exc), "output": str(exc), "error": str(exc)}
    expected = record.get("expectedStatusCodes") or [200]
    ok = response["status"] in expected
    expected_body = record.get("expectedBodySubstring")
    if expected_body:
        ok = ok and expected_body in response["body"]
    error = None
    if not ok:
        error = f"Unexpected status code {response['status']}" if response["status"] not in expected else f"Response body did not contain expected text: {expected_body}"
    output = f"HTTP {response['status']}{chr(10) + response['body'][:500] if response.get('body') else ''}"
    return {"ok": ok, "summary": f"HTTP {response['status']}", "output": output, "status": response["status"], "snippet": response.get("body", "")[:500], "error": error}


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
    if record.get("kind") == "subagent":
        agent_id = meta.get("agent_id") or metadata.get("agent_id") or record.get("sourceId")
        child = await loadChildAgent(agent_id) if isinstance(agent_id, str) and agent_id else None
        if child:
            await saveChildAgent({**child, "recoveryState": "recovered_after_daemon_restart", "lastUpdatedAt": now})
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
    if record.get("kind") == "subagent" and await _recover_completed_subagent_task(task_id, record, emit, record.get("sessionId")):
        return
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
    if existing and existing.get("kind") == "subagent" and await _recover_completed_subagent_task(task_id, existing, emit, session_id):
        return
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


async def _recover_completed_subagent_task(
    task_id: str,
    record: dict[str, Any],
    emit: Callable[[str, dict[str, Any], str | None], Any],
    session_id: str | None,
) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    agent_id = metadata.get("agent_id") or record.get("sourceId")
    child = await loadChildAgent(agent_id) if isinstance(agent_id, str) and agent_id else None
    if not child or _is_active_child_agent_status(child.get("status")):
        return False
    next_record = _build_recovered_subagent_task_record(record, child)
    await saveTaskRecord(next_record)
    meta = await readTaskMeta(task_id) or {}
    await writeTaskMeta(
        task_id,
        {
            **meta,
            "type": "subagent",
            "task_id": task_id,
            "agent_id": child.get("agentId") or agent_id,
            "session_id": child.get("sessionId"),
            "status": next_record.get("status"),
            "updatedAt": next_record.get("updatedAt"),
            "finishedAt": next_record.get("completedAt"),
            "summary": next_record.get("summary"),
            "resultSummary": next_record.get("resultSummary"),
            "completionReason": child.get("completionReason"),
            "waitingReason": child.get("waitingReason"),
            "attachableSessionId": child.get("attachableSessionId") or child.get("sessionId"),
            "retryable": child.get("retryable"),
            "stoppable": child.get("stoppable"),
            "owner": "daemon",
        },
    )
    updated_at = next_record.get("updatedAt") or now_iso()
    await saveChildAgent({**child, "recoveryState": "completed_while_detached", "lastUpdatedAt": updated_at})
    await maybe_await(emit(_event_name_for_task_status(next_record.get("status")), _task_record_event_payload(next_record), session_id or next_record.get("sessionId")))
    return True


async def _mark_subagent_cancelled_from_stop(meta: dict[str, Any], task_id: str, stopped_at: str) -> None:
    agent_id = meta.get("agent_id")
    child = await loadChildAgent(agent_id) if isinstance(agent_id, str) and agent_id else None
    if not child:
        return
    summary = "Stopped by request."
    await saveChildAgent(
        {
            **child,
            "status": "cancelled",
            "completedAt": stopped_at,
            "taskId": task_id,
            "error": summary,
            "completionReason": "cancelled",
            "resultSummary": summary,
            "latestResultAt": stopped_at,
            "retryable": True,
            "stoppable": False,
            "recoveryState": None,
            "lastUpdatedAt": stopped_at,
            "lastSummary": summary,
        }
    )


def _build_recovered_subagent_task_record(record: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    status = _map_child_agent_status_to_task_status(child.get("status"))
    updated_at = child.get("lastUpdatedAt") or child.get("latestResultAt") or child.get("completedAt") or now_iso()
    result_summary = child.get("resultSummary") or child.get("lastSummary") or child.get("error") or child.get("waitingReason") or child.get("stopReason")
    metadata = dict(record.get("metadata") or {})
    metadata.update(
        {
            "agent_id": child.get("agentId"),
            "child_session_id": child.get("sessionId"),
            "waiting_reason": child.get("waitingReason"),
            "completion_reason": child.get("completionReason"),
            "recovery_state": "completed_while_detached",
            "retryable": child.get("retryable"),
            "stoppable": child.get("stoppable"),
        }
    )
    return {
        **record,
        "status": status,
        "title": child.get("name") or child.get("agentType") or child.get("agentId"),
        "summary": _summarize_recovered_subagent(child, result_summary),
        "updatedAt": updated_at,
        "completedAt": child.get("completedAt") or child.get("latestResultAt") or updated_at if status in {"completed", "failed", "stopped"} else None,
        "parentTaskId": child.get("parentTaskId") or record.get("parentTaskId"),
        "relatedChildSessionIds": [child.get("sessionId")] if child.get("sessionId") else [],
        "owner": child.get("owner") or record.get("owner"),
        "taskFamily": "subagent",
        "detailStatus": child.get("completionReason") or child.get("waitingReason") or child.get("status"),
        "attachableSessionId": child.get("attachableSessionId") or child.get("sessionId"),
        "retrieveOnly": False,
        "resultSummary": result_summary,
        "metadata": metadata,
    }


def _build_task_record(meta: dict[str, Any], summary: str | None) -> dict[str, Any]:
    now = now_iso()
    task_id = meta.get("taskId") or meta.get("id")
    output_file = meta.get("outputFile") or taskOutputPath(task_id)
    output_cursor = Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0
    status = _map_status(meta.get("status"))
    is_subagent = meta.get("type") == "subagent"
    is_main_session = meta.get("type") == "main_session"
    is_background_process = meta.get("type") in {"background_command", "main_session", "subagent"}
    if status == "running" and is_background_process and not meta.get("finishedAt") and not meta.get("stoppedAt"):
        status = "backgrounded"
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
    task_family = "shell"
    if meta.get("type") == "health_check":
        task_family = "health_check"
    elif is_subagent:
        task_family = "subagent"
    elif is_main_session:
        task_family = "detached_session"
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
        "taskFamily": task_family,
        "detailStatus": meta.get("detailStatus") or status,
        "retrieveOnly": False if is_subagent or is_main_session else True,
        "attachableSessionId": meta.get("attachableSessionId") if is_subagent or is_main_session else None,
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
        "detail_status": record.get("detailStatus"),
        "foreground_mode": record.get("foregroundMode"),
        "task_family": record.get("taskFamily"),
        "attachable_session_id": record.get("attachableSessionId"),
        "retrieve_only": record.get("retrieveOnly"),
        "result_summary": record.get("resultSummary"),
        "updated_at": record.get("updatedAt"),
    }


def _map_status(value: Any) -> str:
    if value == "active":
        return "running"
    if value == "unhealthy":
        return "degraded"
    if value in ("running", "completed", "failed", "stopped", "queued", "awaiting_input", "reconciling", "degraded"):
        return str(value)
    if value in ("terminated", "cancelled"):
        return "stopped"
    return "running"


def _is_active_child_agent_status(status: Any) -> bool:
    return status in {"queued", "running", "backgrounded"}


def _map_child_agent_status_to_task_status(status: Any) -> str:
    if status in {"queued", "running", "backgrounded", "completed", "failed"}:
        return str(status)
    if status in {"awaiting_input", "awaiting_plan_approval"}:
        return "awaiting_input"
    if status in {"cancelled", "stopped"}:
        return "stopped"
    if status == "truncated":
        return "failed"
    return "idle"


def _summarize_recovered_subagent(child: dict[str, Any], result_summary: Any) -> str:
    if child.get("waitingReason"):
        return str(child["waitingReason"])
    if result_summary:
        return str(result_summary)
    if child.get("status") == "completed":
        return "Subagent completed while the daemon was detached from the worker process."
    if child.get("status") in {"failed", "truncated"}:
        return "Subagent ended with a failure while the daemon was detached from the worker process."
    return "Subagent state was recovered after the daemon reattached to persisted worker records."


def _event_name_for_task_status(status: Any) -> str:
    if status == "completed":
        return "task.completed"
    if status in {"failed", "stopped"}:
        return "task.failed"
    return "task.updated"


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


def _process_exit_summary(status: str, code: int | None) -> str:
    if status == "completed":
        return "Task completed successfully."
    if status == "stopped":
        return "Task was terminated."
    return f"Task failed with exit code {code}." if isinstance(code, int) else "Task failed."


def _next_backoff_interval_ms(record: dict[str, Any], failure_count: int) -> int:
    base = max(250, int(record.get("intervalMs") or 10000))
    multiplier = max(1, float(record.get("backoffMultiplier") or 2))
    max_interval = max(base, int(record.get("maxIntervalMs") or base * 4))
    return min(max_interval, round(base * (multiplier ** max(0, failure_count - 1))))


def _dedupe(values: list[Any]) -> list[Any]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected {field} to be a non-empty string")
    return value
