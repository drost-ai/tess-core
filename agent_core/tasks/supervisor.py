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

from .._utils import maybe_await, now_iso
from ..storage import (
    ensureBaseLayout,
    listTaskRecords,
    listChildAgents,
    listHealthCheckTasks,
    listSessionMonitors,
    listSessionTaskRecords,
    loadBridge,
    loadChildAgent,
    loadHealthCheckTask,
    loadRemoteSession,
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
            "detailStatus": "launching",
            "owner": "daemon",
            "supervisorId": supervisor_id,
        }
        await writeTaskMeta(task_id, meta)
        record = _build_task_record(meta, "Background command launched by daemon.")
        await saveTaskRecord(record)
        session = await loadSession(session_id)
        if session:
            metadata = session.setdefault("metadata", {})
            metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
            await saveSession(session)
        await emit("task.updated", _task_record_event_payload(record), session_id)
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

    async def read_task_output(task_id: str, tailBytes: int | None = 12000) -> dict[str, Any]:
        meta = await readTaskMeta(task_id)
        output_file = (meta or {}).get("outputFile") or taskOutputPath(task_id)
        path = Path(output_file)
        if not path.exists():
            return {"taskId": task_id, "output": "", "truncated": False, "outputFile": output_file}
        data = path.read_bytes()
        truncated = False
        effective_tail_bytes = 12000 if tailBytes is None else max(1, int(tailBytes))
        if len(data) > effective_tail_bytes:
            data = data[-effective_tail_bytes:]
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
        meta_type = meta.get("type") if meta else None
        if proc and proc.poll() is None and meta_type != "subagent":
            return _terminate_process(proc.pid)
        health = await loadHealthCheckTask(task_id)
        if health:
            now = now_iso()
            health.update({"status": "stopped", "updatedAt": now, "stoppedAt": now})
            await saveHealthCheckTask(health)
            await writeTaskMeta(task_id, {**health, "type": "health_check", "updatedAt": now})
            return True
        if meta and meta_type != "subagent" and isinstance(meta.get("pid"), int) and _is_process_alive(meta["pid"]):
            return _terminate_process(meta["pid"])
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
        from ..storage import listTaskRecords

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
        records = await _project_session_task_records(session_id)
        return {
            "taskSnapshot": _task_snapshot(records),
            "monitorSnapshot": _monitor_snapshot(await listSessionMonitors(session_id)),
            "collaborationSnapshot": await _collaboration_snapshot(session_id),
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
            [_resolve_shell(), "-lc", command],
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
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
    package_root = str(Path(__file__).resolve().parents[2])
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
    previous_status = previous.get("status")
    status = "completed" if code == 0 and previous_status not in {"stopped", "terminated"} else "failed"
    if previous_status in {"stopped", "terminated"}:
        status = "terminated" if previous_status == "terminated" else "stopped"
    elif code is not None and code < 0:
        status = "terminated" if abs(code) in {signal.SIGINT, signal.SIGTERM} else "failed"
    exit_code = None if status == "terminated" else code
    meta = {
        **previous,
        "type": previous.get("type") or "background_command",
        "taskId": task_id,
        "id": task_id,
        "command": previous.get("command") or command,
        "cwd": previous.get("cwd") or cwd,
        "outputFile": previous.get("outputFile") or output_file,
        "status": status,
        "exitCode": exit_code,
        "finishedAt": now,
        "updatedAt": now,
        "detailStatus": status,
    }
    asyncio.run(writeTaskMeta(task_id, meta))
    record = _build_task_record(meta, _process_exit_summary(status, code))
    asyncio.run(saveTaskRecord(record))
    event_session_id = meta.get("parent_session_id") if meta.get("type") == "subagent" else meta.get("sessionId")
    asyncio.run(maybe_await(emit("task.completed" if status == "completed" else "task.failed", _task_record_event_payload(record), event_session_id)))
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


async def _project_session_task_records(session_id: str) -> list[dict[str, Any]]:
    session = await loadSession(session_id) or {"id": session_id, "metadata": {}}
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    records_by_id: dict[str, dict[str, Any]] = {}

    for record in await listSessionTaskRecords(session_id):
        if record.get("id"):
            records_by_id[str(record["id"])] = record

    monitors = await listSessionMonitors(session_id)
    task_ids = list(metadata.get("taskIds") or [])
    task_ids.extend(monitor.get("taskId") for monitor in monitors if monitor.get("taskId"))
    task_ids.extend(task.get("taskId") for task in await listHealthCheckTasks() if task.get("sessionId") == session_id and task.get("taskId"))
    for task_id in _dedupe([item for item in task_ids if isinstance(item, str) and item]):
        meta = await readTaskMeta(task_id)
        if meta:
            record = _build_task_record(meta, None, session)
            related_monitor_ids = [monitor.get("id") for monitor in monitors if monitor.get("taskId") == task_id and monitor.get("id")]
            if related_monitor_ids:
                record["relatedMonitorIds"] = related_monitor_ids
            records_by_id[task_id] = record

    for monitor in monitors:
        monitor_id = monitor.get("id")
        if monitor_id:
            records_by_id[f"monitor:{monitor_id}"] = _build_monitor_task_record(session, monitor)

    for child in await listChildAgents():
        if _is_related_child_agent(session, child):
            child_record = await _build_child_agent_task_record(session, child)
            if child_record.get("id"):
                records_by_id[str(child_record["id"])] = child_record

    remote_id = metadata.get("remoteSessionId")
    remote = await loadRemoteSession(remote_id) if isinstance(remote_id, str) and remote_id else None
    if remote:
        bridge = await loadBridge(remote.get("bridgeId")) if isinstance(remote.get("bridgeId"), str) else None
        records_by_id[f"remote:{remote['id']}"] = _build_remote_task_record(session, remote, (bridge or {}).get("status"))

    records = sorted(records_by_id.values(), key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
    await _sync_projected_task_metadata(session, records)
    return records


async def _sync_projected_task_metadata(session: dict[str, Any], records: list[dict[str, Any]]) -> None:
    if not session.get("id"):
        return
    metadata = session.setdefault("metadata", {})
    task_ids = [
        record.get("id")
        for record in records
        if record.get("kind") in {"background_command", "main_session", "health_check"} and record.get("id")
    ]
    restore = {
        "savedAt": now_iso(),
        "activeTasks": [
            {
                "id": record.get("id"),
                "kind": record.get("kind"),
                "status": record.get("status"),
                "title": record.get("title"),
                "summary": record.get("summary"),
                "updatedAt": record.get("updatedAt"),
                "foregroundMode": record.get("foregroundMode"),
                "taskFamily": record.get("taskFamily"),
                "detailStatus": record.get("detailStatus"),
                "attachableSessionId": record.get("attachableSessionId"),
                "retrieveOnly": record.get("retrieveOnly"),
                "resultSummary": record.get("resultSummary"),
            }
            for record in records
            if _is_active_task_status(record.get("status"))
        ][:12],
        "restoredTaskCount": sum(1 for record in records if record.get("restorable")),
        "lastTaskEventAt": records[0].get("updatedAt") if records else None,
    }
    previous_task_ids = list(metadata.get("taskIds") or [])
    previous_restore = metadata.get("runtimeRestore") or {}
    if previous_task_ids == task_ids and _restore_without_saved_at(previous_restore) == _restore_without_saved_at(restore):
        return
    metadata["taskIds"] = task_ids
    metadata["runtimeRestore"] = restore
    await saveSession(session)


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
        "type": record.get("kind"),
        "taskId": task_id,
        "status": "failed",
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


def _build_monitor_task_record(session: dict[str, Any], monitor: dict[str, Any]) -> dict[str, Any]:
    task_id = monitor.get("taskId")
    output_file = taskOutputPath(task_id) if isinstance(task_id, str) else None
    output_cursor = Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0
    return {
        "id": f"monitor:{monitor.get('id')}",
        "sessionId": session.get("id"),
        "sourceId": monitor.get("id"),
        "kind": "monitor",
        "status": _map_monitor_status(monitor.get("status")),
        "title": f"Monitor {monitor.get('goal')}",
        "summary": monitor.get("lastAssistantUpdateSummary") or _summarize_monitor(monitor),
        "createdAt": monitor.get("createdAt"),
        "updatedAt": monitor.get("updatedAt") or monitor.get("createdAt"),
        "startedAt": monitor.get("createdAt"),
        "completedAt": monitor.get("completedAt") or monitor.get("stoppedAt"),
        "foregroundable": False,
        "restorable": True,
        "outputCursor": output_cursor,
        "relatedMonitorIds": [monitor.get("id")] if monitor.get("id") else [],
        "taskFamily": "monitor",
        "detailStatus": monitor.get("status"),
        "retrieveOnly": True,
        "resultSummary": monitor.get("lastAssistantUpdateSummary"),
        "metadata": {
            "task_id": monitor.get("taskId"),
            "goal": monitor.get("goal"),
            "last_task_status": monitor.get("lastObservedTaskStatus"),
            "muted": monitor.get("muted"),
        },
    }


async def _build_child_agent_task_record(session: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    task_id = child.get("taskId")
    task_meta = await readTaskMeta(task_id) if isinstance(task_id, str) and task_id else None
    child_session = await loadSession(child.get("sessionId")) if isinstance(child.get("sessionId"), str) else None
    latest_assistant = _summarize_latest_message(child_session or {}, "assistant")
    latest_user = _summarize_latest_message(child_session or {}, "user")
    result_summary = child.get("resultSummary") or child.get("lastSummary") or latest_assistant or child.get("error") or child.get("waitingReason")
    owner = child.get("owner") or ((task_meta or {}).get("owner") if (task_meta or {}).get("owner") in {"daemon", "client"} else None)
    return {
        "id": task_id or child.get("agentId"),
        "sessionId": session.get("id"),
        "sourceId": child.get("agentId"),
        "kind": "subagent",
        "status": _map_child_agent_status_to_task_status(child.get("status")),
        "title": child.get("name") or child.get("agentType") or child.get("agentId"),
        "summary": _summarize_child_agent_task(child, latest_assistant, latest_user, result_summary),
        "createdAt": child.get("startedAt"),
        "updatedAt": child.get("lastUpdatedAt") or child.get("completedAt") or child.get("startedAt"),
        "startedAt": child.get("startedAt"),
        "completedAt": child.get("completedAt"),
        "foregroundable": True,
        "restorable": True,
        "outputCursor": _output_size(taskOutputPath(task_id)) if isinstance(task_id, str) else None,
        "outputFile": taskOutputPath(task_id) if isinstance(task_id, str) else None,
        "parentTaskId": child.get("parentTaskId"),
        "relatedChildSessionIds": [child.get("sessionId")] if child.get("sessionId") else [],
        "owner": owner,
        "foregroundMode": "background" if child.get("runInBackground") else "foreground",
        "taskFamily": "subagent",
        "detailStatus": _describe_child_agent_detail_status(child),
        "attachableSessionId": child.get("attachableSessionId") or child.get("sessionId"),
        "retrieveOnly": False,
        "resultSummary": result_summary,
        "metadata": {
            "agent_id": child.get("agentId"),
            "child_session_id": child.get("sessionId"),
            "task_id": task_id,
            "worktree_path": child.get("worktreePath"),
            "pid": child.get("pid"),
            "waiting_reason": child.get("waitingReason"),
            "completion_reason": child.get("completionReason"),
            "retryable": child.get("retryable"),
            "stoppable": child.get("stoppable"),
            "owner": owner,
            "recovery_state": child.get("recoveryState"),
            "daemon_status": (task_meta or {}).get("status") if isinstance((task_meta or {}).get("status"), str) else None,
        },
    }


def _build_remote_task_record(session: dict[str, Any], remote: dict[str, Any], bridge_status: Any = None) -> dict[str, Any]:
    return {
        "id": f"remote:{remote.get('id')}",
        "sessionId": session.get("id"),
        "sourceId": remote.get("id"),
        "kind": "remote",
        "status": _map_remote_status(remote.get("status")),
        "title": remote.get("title"),
        "summary": remote.get("lastEventSummary"),
        "createdAt": remote.get("createdAt"),
        "updatedAt": remote.get("updatedAt"),
        "startedAt": remote.get("createdAt"),
        "completedAt": remote.get("updatedAt") if remote.get("status") in {"completed", "failed", "closed"} else None,
        "foregroundable": True,
        "restorable": True,
        "foregroundMode": "foreground" if remote.get("status") == "active" else "background",
        "taskFamily": "remote",
        "detailStatus": remote.get("status"),
        "attachableSessionId": remote.get("foregroundSessionId") or remote.get("sessionId"),
        "retrieveOnly": False,
        "resultSummary": remote.get("lastEventSummary"),
        "metadata": {
            "remote_session_id": remote.get("id"),
            "bridge_id": remote.get("bridgeId"),
            "bridge_status": bridge_status,
            "foreground_session_id": remote.get("foregroundSessionId"),
        },
    }


def _build_task_record(meta: dict[str, Any], summary: str | None, session: dict[str, Any] | None = None) -> dict[str, Any]:
    now = now_iso()
    task_id = meta.get("taskId") or meta.get("task_id") or meta.get("id")
    output_file = meta.get("outputFile") or taskOutputPath(task_id)
    output_cursor = Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0
    output_preview = _read_output_preview(output_file)
    task_type = meta.get("type") or "background_command"
    status = _status_for_task_meta(meta)
    is_subagent = meta.get("type") == "subagent"
    is_main_session = meta.get("type") == "main_session"
    is_health_check = meta.get("type") == "health_check"
    session_data = session or {}
    latest_assistant = _summarize_latest_message(session_data, "assistant") if is_main_session else None
    latest_user = _summarize_latest_message(session_data, "user") if is_main_session else None
    if summary is None:
        summary = _summary_for_task_meta(meta, status, output_cursor, output_preview, latest_assistant, latest_user)
    metadata = dict(meta.get("metadata") or {})
    metadata.update({"pid": meta.get("pid"), "exitCode": meta.get("exitCode"), "exit_code": meta.get("exitCode"), "lastResult": meta.get("lastResult")})
    if not is_health_check:
        metadata.update(
            {
                "runner_pid": meta.get("runnerPid"),
                "detached_type": "main_session" if is_main_session else "background_command" if not is_subagent else None,
                "output_preview": output_preview,
                "recovery_state": "recovered_after_daemon_restart" if meta.get("status") == "reconciling" else metadata.get("recovery_state"),
            }
        )
    if is_health_check:
        metadata.update(
            {
                "mode": meta.get("mode"),
                "interval_ms": meta.get("intervalMs"),
                "current_interval_ms": meta.get("currentIntervalMs") or meta.get("intervalMs"),
                "timeout_ms": meta.get("timeoutMs"),
                "backoff_multiplier": meta.get("backoffMultiplier"),
                "max_interval_ms": meta.get("maxIntervalMs"),
                "failure_threshold": meta.get("failureThreshold"),
                "consecutive_failures": meta.get("consecutiveFailures") or 0,
                "last_status_code": meta.get("lastStatusCode"),
                "last_exit_code": meta.get("lastExitCode"),
                "expected_body_substring": meta.get("expectedBodySubstring"),
            }
        )
    if is_main_session:
        metadata.update(
            {
                "latest_assistant_summary": latest_assistant,
                "latest_user_prompt": latest_user,
                "session_updated_at": session_data.get("updatedAt"),
            }
        )
    if is_subagent:
        metadata.update(
            {
                "agent_id": meta.get("agent_id"),
                "child_session_id": meta.get("session_id") or meta.get("sessionId"),
                "parent_session_id": meta.get("parent_session_id"),
                "parent_task_id": meta.get("parent_task_id"),
            }
        )
    task_family = "health_check" if is_health_check else "subagent" if is_subagent else "detached_session" if is_main_session else "shell"
    kind = "health_check" if is_health_check else "subagent" if is_subagent else "main_session" if is_main_session else "background_command"
    session_id = meta.get("parent_session_id") if is_subagent else meta.get("sessionId") or session_data.get("id")
    updated_at = meta.get("updatedAt") or meta.get("finishedAt") or meta.get("startedAt") or now
    if is_main_session:
        updated_at = _latest_timestamp(session_data.get("updatedAt"), updated_at)
    return {
        "id": task_id,
        "sessionId": session_id,
        "sourceId": meta.get("agent_id") if is_subagent else task_id,
        "kind": kind,
        "status": status,
        "title": _first_text(meta.get("title"), meta.get("description"), _summarize_command(meta.get("command")), task_id),
        "summary": summary or meta.get("error") or meta.get("detailStatus") or status,
        "createdAt": meta.get("createdAt") or meta.get("startedAt") or now,
        "updatedAt": updated_at,
        "startedAt": meta.get("startedAt") or meta.get("createdAt"),
        "completedAt": meta.get("updatedAt") if is_health_check and meta.get("status") == "stopped" else meta.get("finishedAt") or meta.get("stoppedAt"),
        "foregroundable": False if is_health_check else True,
        "restorable": True,
        "cwd": meta.get("cwd"),
        "command": meta.get("command") or meta.get("url") if is_health_check else meta.get("command"),
        "outputFile": output_file,
        "outputCursor": output_cursor,
        "relatedMonitorIds": meta.get("relatedMonitorIds") or [],
        "foregroundMode": "background",
        "taskFamily": task_family,
        "detailStatus": _detail_status_for_task_meta(meta, status, output_preview, latest_assistant),
        "retrieveOnly": False if is_subagent or is_main_session else True,
        "attachableSessionId": (meta.get("attachableSessionId") or session_data.get("id")) if is_main_session else meta.get("attachableSessionId") if is_subagent else None,
        "parentTaskId": meta.get("parent_task_id") if is_subagent else None,
        "relatedChildSessionIds": [meta.get("session_id") or meta.get("sessionId")] if is_subagent and (meta.get("session_id") or meta.get("sessionId")) else [],
        "owner": meta.get("owner") or "daemon",
        "supervisorId": meta.get("supervisorId"),
        "resultSummary": _result_summary_for_task_meta(meta, status, output_preview, latest_assistant, latest_user),
        "metadata": metadata,
    }


def _status_for_task_meta(meta: dict[str, Any]) -> str:
    task_type = meta.get("type")
    if task_type == "health_check":
        status = meta.get("status")
        if status == "active":
            return "running"
        if status == "unhealthy":
            return "degraded"
        if status == "stopped":
            return "stopped"
        return "failed"
    status = _map_status(meta.get("status"))
    if task_type in {"background_command", "main_session", "subagent"} and status == "running" and not meta.get("finishedAt") and not meta.get("stoppedAt"):
        return "backgrounded"
    return status


def _summary_for_task_meta(
    meta: dict[str, Any],
    status: str,
    output_cursor: int,
    output_preview: str | None,
    latest_assistant: str | None,
    latest_user: str | None,
) -> str | None:
    task_type = meta.get("type")
    if task_type == "health_check":
        return _summarize_health_check_task(meta)
    if task_type == "main_session":
        return _summarize_detached_main_session_task(status, latest_assistant, latest_user, output_preview)
    if task_type == "subagent":
        return meta.get("summary") or meta.get("error") or meta.get("detailStatus") or status
    return meta.get("error") or _summarize_background_command_task(meta, status, output_cursor, output_preview)


def _detail_status_for_task_meta(meta: dict[str, Any], status: str, output_preview: str | None, latest_assistant: str | None) -> str:
    task_type = meta.get("type")
    if task_type == "health_check":
        return "unhealthy" if meta.get("status") == "unhealthy" else str(meta.get("status") or status)
    if task_type == "main_session":
        return _describe_detached_main_session_detail_status(status, latest_assistant)
    if task_type == "subagent":
        return str(meta.get("detailStatus") or meta.get("completionReason") or meta.get("waitingReason") or status)
    return _describe_background_command_detail_status(meta, status, output_preview)


def _result_summary_for_task_meta(
    meta: dict[str, Any],
    status: str,
    output_preview: str | None,
    latest_assistant: str | None,
    latest_user: str | None,
) -> str | None:
    task_type = meta.get("type")
    if task_type == "health_check":
        return _first_text(meta.get("lastError"), meta.get("lastResponseSnippet"))
    if task_type == "main_session":
        return _build_detached_main_session_result_summary(status, latest_assistant, latest_user)
    if task_type == "subagent":
        return _first_text(meta.get("resultSummary"), meta.get("summary"), meta.get("error"), meta.get("waitingReason"), meta.get("stopReason"))
    return _build_background_command_result_summary(meta, output_preview)


def _summarize_health_check_task(meta: dict[str, Any]) -> str:
    if meta.get("lastError"):
        return f"{'Check unhealthy' if meta.get('status') == 'unhealthy' else 'Last check failed'}: {meta.get('lastError')}"
    if isinstance(meta.get("lastStatusCode"), int):
        return f"Last HTTP status {meta.get('lastStatusCode')}"
    if isinstance(meta.get("lastExitCode"), int):
        return f"Last exit code {meta.get('lastExitCode')}"
    return "Scheduled health check active."


def _summarize_background_command_task(meta: dict[str, Any], status: str, output_cursor: int, output_preview: str | None) -> str | None:
    if meta.get("status") == "reconciling" or status == "reconciling":
        return "Recovered after daemon restart. Process still appears alive, but lifecycle tracking is degraded."
    if status in {"backgrounded", "running"}:
        return f"Background command is active. Latest output: {output_preview}" if output_preview else f"{_format_relative_path(str(meta.get('cwd') or ''))} · {output_cursor} bytes output"
    if status == "completed":
        return f"Background command completed successfully. Latest output: {output_preview}" if output_preview else "Background command completed successfully."
    if status == "stopped":
        return f"Background command was stopped. Latest output: {output_preview}" if output_preview else "Background command was stopped."
    if meta.get("error"):
        return str(meta.get("error"))
    if isinstance(meta.get("exitCode"), int):
        return f"Background command failed with exit code {meta.get('exitCode')}."
    return output_preview


def _build_background_command_result_summary(meta: dict[str, Any], output_preview: str | None) -> str | None:
    for value in (meta.get("resultSummary"), meta.get("error"), output_preview):
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(meta.get("exitCode"), int):
        return f"Exited with code {meta.get('exitCode')}."
    return None


def _describe_background_command_detail_status(meta: dict[str, Any], status: str, output_preview: str | None) -> str:
    if meta.get("status") == "reconciling" or status == "reconciling":
        return "recovered_after_daemon_restart"
    if status in {"backgrounded", "running"}:
        return "streaming_output" if output_preview else "launching"
    if status == "completed":
        return "result_ready"
    if status == "stopped":
        return "stopped"
    if status == "failed":
        return "failed"
    return status


def _summarize_detached_main_session_task(
    status: str,
    latest_assistant: str | None,
    latest_user: str | None,
    output_preview: str | None,
) -> str | None:
    if status == "reconciling":
        return "Recovered after daemon restart. The detached session is still alive, but lifecycle tracking is degraded."
    if status in {"backgrounded", "running"}:
        if latest_assistant:
            return f"This session is continuing in the background. Latest assistant state: {latest_assistant}"
        if latest_user:
            return f"This session is continuing in the background. Current work: {latest_user}"
        return f"This session is continuing in the background. Runtime output: {output_preview}" if output_preview else "This session is continuing in the background."
    if status == "completed":
        return f"Detached session completed. Latest assistant result: {latest_assistant}" if latest_assistant else "Detached session completed."
    if status == "stopped":
        return f"Detached session stopped. Latest assistant state: {latest_assistant}" if latest_assistant else "Detached session stopped."
    if status == "failed":
        return f"Detached session failed. Latest assistant state: {latest_assistant}" if latest_assistant else "Detached session failed."
    return latest_assistant or latest_user or output_preview


def _build_detached_main_session_result_summary(status: str, latest_assistant: str | None, latest_user: str | None) -> str | None:
    if latest_assistant:
        return latest_assistant
    if status in {"backgrounded", "running"} and latest_user:
        return f"Continuing work on: {latest_user}"
    return None


def _describe_detached_main_session_detail_status(status: str, latest_assistant: str | None) -> str:
    if status == "reconciling":
        return "recovered_after_daemon_restart"
    if status in {"backgrounded", "running"}:
        return "continuing_session" if latest_assistant else "session_starting"
    if status == "completed":
        return "result_ready"
    if status == "stopped":
        return "stopped"
    if status == "failed":
        return "failed"
    return status


def _read_output_preview(output_file: str | None, tail_bytes: int = 512) -> str | None:
    if not output_file:
        return None
    try:
        path = Path(output_file)
        size = path.stat().st_size
        if size <= 0:
            return None
        bytes_to_read = min(size, max(64, tail_bytes))
        with path.open("rb") as handle:
            handle.seek(max(0, size - bytes_to_read))
            data = handle.read(bytes_to_read)
        lines = [_collapse_whitespace(line) for line in data.decode("utf-8", errors="replace").splitlines()]
        preview = next((line for line in reversed(lines) if line), None)
        return _truncate(preview, 180) if preview else None
    except Exception:
        return None


def _latest_timestamp(left: Any, right: Any) -> str:
    if not isinstance(left, str) or not left:
        return str(right or now_iso())
    if not isinstance(right, str) or not right:
        return left
    left_ms = _parse_iso_ms(left)
    right_ms = _parse_iso_ms(right)
    if left_ms is None or right_ms is None:
        return left if left >= right else right
    return left if left_ms >= right_ms else right


def _format_relative_path(value: str) -> str:
    home = os.environ.get("HOME")
    if home and value.startswith(home):
        return f"~{os.sep}{os.path.relpath(value, home)}"
    return value


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max(0, max_length - 3)].rstrip()}..."


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


async def _collaboration_snapshot(session_id: str) -> dict[str, Any]:
    session = await loadSession(session_id)
    if not session:
        return _empty_collaboration_snapshot()
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    remote_id = metadata.get("remoteSessionId")
    remote = await loadRemoteSession(remote_id) if isinstance(remote_id, str) and remote_id else None
    bridge_id = metadata.get("bridgeId") or (remote or {}).get("bridgeId")
    bridge = await loadBridge(bridge_id) if isinstance(bridge_id, str) and bridge_id else None
    from ..collaboration import buildCollaborationWorkers, createCollaborationSnapshot

    workers = buildCollaborationWorkers(
        {
            "session": session,
            "childAgents": await listChildAgents(),
            "monitors": await listSessionMonitors(session_id),
            "remote": remote,
            "bridge": bridge,
        }
    )
    return createCollaborationSnapshot(workers, remote, bridge)


def _is_active_task_status(status: Any) -> bool:
    return status in {"queued", "running", "backgrounded", "awaiting_input", "reconciling", "degraded"}


def _restore_without_saved_at(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key != "savedAt"}


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
    return "idle"


def _map_monitor_status(status: Any) -> str:
    if status == "active":
        return "running"
    if status in {"completed", "failed"}:
        return str(status)
    if status in {"stopped", "muted"}:
        return "stopped"
    return "idle"


def _map_remote_status(status: Any) -> str:
    if status in {"created", "active"}:
        return "running"
    if status == "idle":
        return "idle"
    if status == "awaiting_input":
        return "awaiting_input"
    if status == "completed":
        return "completed"
    if status in {"failed", "closed"}:
        return "failed"
    return "idle"


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


def _summarize_monitor(monitor: dict[str, Any]) -> str:
    parts = []
    if monitor.get("lastObservedTaskStatus"):
        parts.append(f"task {monitor.get('lastObservedTaskStatus')}")
    if monitor.get("lastObservedOutputSize") is not None:
        parts.append(f"{monitor.get('lastObservedOutputSize')} bytes observed")
    return " · ".join(parts) if parts else str(monitor.get("goal") or "")


def _summarize_child_agent_task(child: dict[str, Any], latest_assistant: str | None, latest_user: str | None, result_summary: Any) -> str | None:
    if child.get("waitingReason"):
        return str(child["waitingReason"])
    if child.get("status") in {"backgrounded", "running"} and latest_assistant:
        return f"Worker is active. Latest state: {latest_assistant}"
    if child.get("status") in {"backgrounded", "running"} and latest_user:
        return f"Worker is active on: {latest_user}"
    if child.get("status") == "completed" and result_summary:
        return f"Worker completed. {result_summary}"
    if child.get("status") in {"failed", "truncated"} and result_summary:
        return f"Worker ended with a problem. {result_summary}"
    if child.get("status") in {"cancelled", "stopped"} and result_summary:
        return f"Worker stopped. {result_summary}"
    return result_summary or child.get("lastSummary") or child.get("error") or child.get("stopReason")


def _describe_child_agent_detail_status(child: dict[str, Any]) -> str | None:
    if child.get("recoveryState") == "recovered_after_daemon_restart":
        return "recovered_after_daemon_restart"
    if child.get("recoveryState") == "completed_while_detached":
        return "completed_while_detached"
    return child.get("status") if child.get("waitingReason") else child.get("completionReason") or child.get("status")


def _is_related_child_agent(session: dict[str, Any], child: dict[str, Any]) -> bool:
    if child.get("parentSessionId") == session.get("id") or child.get("sessionId") == session.get("id"):
        return True
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    child_sessions = set(metadata.get("childSessionIds") or [])
    background_sessions = set(metadata.get("backgroundSessionIds") or [])
    child_agents = set(metadata.get("childAgentIds") or [])
    return child.get("sessionId") in child_sessions or child.get("sessionId") in background_sessions or child.get("agentId") in child_agents


def _summarize_latest_message(session: dict[str, Any], role: str) -> str | None:
    max_length = 220 if role == "assistant" else 180
    for message in reversed(session.get("messages") or []):
        if message.get("role") != role:
            continue
        text = _message_text(message.get("content")).strip()
        if text:
            return _truncate(text, max_length)
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        return " ".join(" ".join(parts).split())
    return ""


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


def _output_size(output_file: str | None) -> int:
    return Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0


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
    return text if len(text) <= 72 else f"{text[:71].rstrip()}..."


def _resolve_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"


def _process_exit_summary(status: str, code: int | None) -> str:
    if status == "completed":
        return "Task completed successfully."
    if status in {"stopped", "terminated"}:
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
