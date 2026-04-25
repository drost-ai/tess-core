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
    loadHealthCheckTask,
    loadTaskRecord,
    readTaskMeta,
    saveHealthCheckTask,
    saveTaskRecord,
    taskOutputPath,
    writeTaskMeta,
)


_PROCESS_REGISTRY: dict[str, subprocess.Popen[Any]] = {}
_WATCHERS: set[threading.Thread] = set()


async def createTaskSupervisor(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd = args.get("cwd") or os.getcwd()
    on_event: Callable[[dict[str, Any]], Any] = args.get("onEvent") or (lambda _event: None)
    await ensureBaseLayout(cwd)
    supervisor_id = str(uuid.uuid4())
    subscribers: set[Callable[[dict[str, Any]], Any]] = set()

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
            "owner": "python",
            "supervisorId": supervisor_id,
        }
        await writeTaskMeta(task_id, meta)
        await saveTaskRecord(_build_task_record(meta, "Background command is running."))
        await emit("task.updated", _task_event_payload(meta), session_id)
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
            now = now_iso()
            meta.update({"status": "stopped", "updatedAt": now, "stoppedAt": now, "detailStatus": "stopped"})
            await writeTaskMeta(task_id, meta)
            await saveTaskRecord(_build_task_record(meta, "Task stopped by request."))
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

    def subscribe(listener: Callable[[dict[str, Any]], Any]):
        subscribers.add(listener)

        def unsubscribe() -> None:
            subscribers.discard(listener)

        return unsubscribe

    async def close() -> None:
        for proc in list(_PROCESS_REGISTRY.values()):
            if proc.poll() is None:
                _terminate_process(proc.pid)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
        for watcher in list(_WATCHERS):
            if watcher is not threading.current_thread() and watcher.is_alive():
                watcher.join(timeout=2)
        _PROCESS_REGISTRY.clear()

    def get_state() -> dict[str, Any]:
        return {
            "supervisorId": supervisor_id,
            "watchedSessions": 0,
            "activeTasks": sum(1 for proc in _PROCESS_REGISTRY.values() if proc.poll() is None),
            "attachedClients": len(subscribers),
        }

    async def get_bootstrap(_session_id: str) -> dict[str, Any]:
        return {"taskSnapshot": None, "monitorSnapshot": None, "collaborationSnapshot": None}

    return {
        "close": close,
        "addAttachment": lambda _session_id=None: (lambda: None),
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
        "startSubagentTask": lambda task_args=None, **task_kwargs: start_background_command(
            {
                **(task_args or {}),
                **task_kwargs,
                "sessionId": (task_args or {}).get("parentSessionId") or task_kwargs.get("parentSessionId"),
                "command": "printf 'Background subagent execution requires the product CLI runner.\\n'",
                "title": (task_args or {}).get("label") or task_kwargs.get("label") or (task_args or {}).get("agentId") or task_kwargs.get("agentId") or "subagent",
            }
        ),
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
    asyncio.run(maybe_await(emit("task.completed" if status == "completed" else "task.failed", _task_event_payload(meta), meta.get("sessionId"))))
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


def _build_task_record(meta: dict[str, Any], summary: str | None) -> dict[str, Any]:
    now = now_iso()
    task_id = meta.get("taskId") or meta.get("id")
    output_file = meta.get("outputFile") or taskOutputPath(task_id)
    output_cursor = Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0
    status = _map_status(meta.get("status"))
    return {
        "id": task_id,
        "sessionId": meta.get("sessionId"),
        "sourceId": task_id,
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
        "taskFamily": "health_check" if meta.get("type") == "health_check" else "shell",
        "detailStatus": meta.get("detailStatus") or status,
        "retrieveOnly": True,
        "owner": meta.get("owner") or "python",
        "supervisorId": meta.get("supervisorId"),
        "metadata": {"pid": meta.get("pid"), "exitCode": meta.get("exitCode"), "lastResult": meta.get("lastResult")},
    }


def _task_event_payload(meta: dict[str, Any]) -> dict[str, Any]:
    record = _build_task_record(meta, None)
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
    if value in ("running", "completed", "failed", "stopped", "queued", "awaiting_input"):
        return str(value)
    if value in ("terminated", "cancelled"):
        return "stopped"
    return "running"


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
