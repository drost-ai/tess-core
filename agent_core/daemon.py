from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from ._utils import now_iso
from .storage import (
    appendDaemonEvent,
    clearDaemonState,
    daemonLogsDir,
    daemonPidPath,
    daemonSocketPath,
    ensureBaseLayout,
    getStateRoot,
    loadDaemonEvents,
    loadDaemonState,
    saveDaemonState,
)
from .task_supervisor import createTaskSupervisor


_SUPERVISOR: dict[str, Any] | None = None
_DAEMON_PROCESS: subprocess.Popen[Any] | None = None


async def runTaskSupervisorDaemon(cwd: str) -> None:
    await ensureBaseLayout(cwd)
    socket_path = daemonSocketPath()
    await _cleanup_stale_daemon_artifacts()
    try:
        os.remove(socket_path)
    except FileNotFoundError:
        pass

    subscribers: dict[str, dict[str, Any]] = {}
    cursor = _last_daemon_event_cursor(await loadDaemonEvents())
    daemon_epoch = str(uuid.uuid4())
    daemon_started_at = now_iso()
    daemon_phase = "recovering"
    supervisor: dict[str, Any] | None = None

    async def write_daemon_state(phase: str | None = None) -> None:
        nonlocal daemon_phase
        if phase:
            daemon_phase = phase
        supervisor_state = supervisor["getState"]() if supervisor else {}
        await saveDaemonState(
            {
                "instanceId": "local-daemon",
                "phase": daemon_phase,
                "epoch": daemon_epoch,
                "pid": os.getpid(),
                "cwd": cwd,
                "socketPath": socket_path,
                "startedAt": daemon_started_at,
                "updatedAt": now_iso(),
                "mode": "detached_python",
                "status": "running" if daemon_phase == "running" else daemon_phase,
                "supervisorId": supervisor_state.get("supervisorId"),
                "attachedClients": supervisor_state.get("attachedClients", 0),
                "watchedSessions": supervisor_state.get("watchedSessions", 0),
                "activeTasks": supervisor_state.get("activeTasks", 0),
            }
        )

    def next_event_cursor() -> int:
        nonlocal cursor
        cursor += 1
        return cursor

    async def on_event(event: dict[str, Any]) -> None:
        nonlocal cursor
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        cursor = next_event_cursor()
        payload = {
            "cursor": cursor,
            "createdAt": now_iso(),
            "taskId": data.get("task_id") if isinstance(data.get("task_id"), str) else None,
            **event,
        }
        await appendDaemonEvent(payload)
        await _publish_daemon_event(subscribers, payload)
        await write_daemon_state()

    supervisor = await createTaskSupervisor({"cwd": cwd, "onEvent": on_event})
    shutdown = asyncio.Event()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        subscriber_id: str | None = None
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line.decode("utf-8"))
                except Exception as exc:
                    response = {"id": None, "ok": False, "error": str(exc)}
                    writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
                    await writer.drain()
                    continue
                if _normalise_daemon_method(request.get("method")) == "subscribe":
                    params = request.get("params") or {}
                    subscriber_id = str(request.get("id") or uuid.uuid4())
                    session_id = params.get("sessionId") or params.get("session_id")
                    release_attachment = supervisor["addAttachment"](session_id)
                    subscribers[subscriber_id] = {
                        "writer": writer,
                        "sessionId": session_id,
                        "releaseAttachment": release_attachment,
                    }
                    writer.write(
                        json.dumps({"id": request.get("id"), "ok": True, "result": {"subscribed": True}}, ensure_ascii=False).encode("utf-8")
                        + b"\n"
                    )
                    await writer.drain()
                    await _replay_daemon_events(writer, session_id, int(params.get("afterCursor") or params.get("after_cursor") or 0))
                    await _send_bootstrap_snapshots(writer, supervisor, session_id, next_event_cursor)
                    await write_daemon_state()
                    await _wait_for_subscription_close(reader, shutdown)
                    break

                response = await _handle_daemon_request(supervisor, shutdown, request)
                writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
                await writer.drain()
                if response.get("shutdown"):
                    break
        finally:
            if subscriber_id:
                subscriber = subscribers.pop(subscriber_id, None)
                release = subscriber.get("releaseAttachment") if subscriber else None
                if callable(release):
                    release()
                await write_daemon_state()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle_client, path=socket_path)
    await _record_daemon_lifecycle_event("daemon.recovering", {"phase": daemon_phase, "epoch": daemon_epoch}, next_event_cursor)
    await write_daemon_state("recovering")
    await write_daemon_state("running")
    await _record_daemon_lifecycle_event("daemon.recovered", {"phase": daemon_phase, "epoch": daemon_epoch}, next_event_cursor)
    try:
        async with server:
            await shutdown.wait()
    finally:
        await write_daemon_state("shutting_down")
        server.close()
        await server.wait_closed()
        for subscriber in list(subscribers.values()):
            release = subscriber.get("releaseAttachment")
            if callable(release):
                release()
            writer = subscriber.get("writer")
            if writer:
                writer.close()
        await supervisor["close"]()
        await clearDaemonState()


async def connectTaskSupervisorDaemonClient(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
    args = {**(args or {}), **kwargs}
    socket_path = args.get("socketPath") or daemonSocketPath()
    state = await loadDaemonState()
    pid = state.get("pid") if state else None
    if isinstance(pid, int) and not _is_process_alive(pid):
        await _cleanup_stale_daemon_artifacts()
        return _SUPERVISOR if _SUPERVISOR and not _SUPERVISOR.get("_daemonClient") else None
    if not os.path.exists(socket_path):
        if state:
            await _cleanup_stale_daemon_artifacts()
        return _SUPERVISOR if _SUPERVISOR and not _SUPERVISOR.get("_daemonClient") else None
    try:
        client = _create_daemon_client(socket_path)
        await client["_request"]("getState", {})
        return client
    except Exception:
        return _SUPERVISOR


async def ensureTaskSupervisorDaemon(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    global _SUPERVISOR
    args = {**(args or {}), **kwargs}
    cwd = args.get("cwd") or os.getcwd()
    if args.get("inProcess"):
        return await _ensure_in_process_supervisor(cwd)

    await _cleanup_stale_daemon_artifacts()
    existing = await connectTaskSupervisorDaemonClient()
    if existing is not None:
        _SUPERVISOR = existing
        return existing

    await _spawn_detached_daemon(cwd)
    deadline = asyncio.get_running_loop().time() + float(args.get("startupTimeoutSeconds") or 5)
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        try:
            client = await connectTaskSupervisorDaemonClient()
            if client is not None:
                _SUPERVISOR = client
                return client
        except Exception as exc:
            last_error = exc
    if args.get("allowInProcessFallback", True):
        return await _ensure_in_process_supervisor(cwd)
    raise RuntimeError(f"Unable to start task supervisor daemon{f': {last_error}' if last_error else ''}")


async def stopTaskSupervisorDaemon() -> None:
    global _SUPERVISOR, _DAEMON_PROCESS
    client = _SUPERVISOR if _SUPERVISOR and _SUPERVISOR.get("_daemonClient") else await connectTaskSupervisorDaemonClient()
    if client and client.get("_daemonClient"):
        try:
            await client["_request"]("shutdown", {})
        except Exception:
            pass
    elif _SUPERVISOR:
        await _SUPERVISOR["close"]()
    state = await loadDaemonState()
    pid = state.get("pid") if state else None
    if isinstance(pid, int) and pid != os.getpid():
        await _wait_for_process_exit(pid, 2)
        if _is_process_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    if _DAEMON_PROCESS and _DAEMON_PROCESS.poll() is None:
        _DAEMON_PROCESS.terminate()
        try:
            _DAEMON_PROCESS.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _DAEMON_PROCESS.kill()
    _SUPERVISOR = None
    _DAEMON_PROCESS = None
    await clearDaemonState()


async def getTaskSupervisorDaemonState() -> dict[str, Any] | None:
    state = await loadDaemonState()
    client = await connectTaskSupervisorDaemonClient()
    if state and client:
        try:
            state = {**state, "supervisor": await client["_request"]("getState", {})}
        except Exception:
            pass
    return state


async def _ensure_in_process_supervisor(cwd: str) -> dict[str, Any]:
    global _SUPERVISOR
    if _SUPERVISOR is not None and not _SUPERVISOR.get("_daemonClient"):
        return _SUPERVISOR

    async def on_event(event: dict[str, Any]) -> None:
        await appendDaemonEvent({"createdAt": now_iso(), **event})

    _SUPERVISOR = await createTaskSupervisor({"cwd": cwd, "onEvent": on_event})
    await saveDaemonState({"pid": os.getpid(), "cwd": cwd, "startedAt": now_iso(), "mode": "in_process_python", "status": "running"})
    return _SUPERVISOR


async def _spawn_detached_daemon(cwd: str) -> None:
    global _DAEMON_PROCESS
    await ensureBaseLayout(cwd)
    os.makedirs(daemonLogsDir(), exist_ok=True)
    log_path = os.path.join(daemonLogsDir(), "daemon.log")
    env = dict(os.environ)
    env["AGENT_CORE_HOME"] = getStateRoot()
    repo_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = repo_root if not env.get("PYTHONPATH") else f"{repo_root}{os.pathsep}{env['PYTHONPATH']}"
    stdout = open(log_path, "ab")
    try:
        _DAEMON_PROCESS = subprocess.Popen(
            [sys.executable, "-m", "agent_core.daemon_worker", "--cwd", cwd],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        stdout.close()
    os.makedirs(os.path.dirname(daemonPidPath()), exist_ok=True)
    with open(daemonPidPath(), "w", encoding="utf-8") as handle:
        handle.write(f"{_DAEMON_PROCESS.pid}\n")


def _create_daemon_client(socket_path: str) -> dict[str, Any]:
    async def request(method: str, params: dict[str, Any] | None = None) -> Any:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        request_id = str(uuid.uuid4())
        writer.write(json.dumps({"id": request_id, "method": method, "params": params or {}}, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        if not line:
            raise RuntimeError("Daemon closed the connection without a response.")
        response = json.loads(line.decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "Daemon request failed.")
        return response.get("result")

    async def start_background_command(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, Any]:
        return await request("startBackgroundCommand", {**(task_args or {}), **task_kwargs})

    async def start_main_session_task(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, Any]:
        return await request("startMainSessionTask", {**(task_args or {}), **task_kwargs})

    async def start_subagent_task(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, Any]:
        return await request("startSubagentTask", {**(task_args or {}), **task_kwargs})

    async def start_health_check(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, Any]:
        return await request("startHealthCheck", {**(task_args or {}), **task_kwargs})

    async def list_tasks(session_id: str | None = None) -> list[dict[str, Any]]:
        return await request("listTasks", {"sessionId": session_id})

    async def inspect_task(task_id: str) -> dict[str, Any] | None:
        return await request("inspectTask", {"taskId": task_id})

    async def read_task_output(task_id: str, tailBytes: int | None = None) -> dict[str, Any]:
        return await request("readTaskOutput", {"taskId": task_id, "tailBytes": tailBytes})

    async def stop_task(task_id: str) -> bool:
        return await request("stopTask", {"taskId": task_id})

    async def retry_task(task_id: str) -> dict[str, Any]:
        return await request("retryTask", {"taskId": task_id})

    async def get_bootstrap(session_id: str) -> dict[str, Any]:
        return await request("getBootstrap", {"sessionId": session_id})

    def subscribe(*args: Any, **kwargs: Any):
        listener, session_id, after_cursor = _normalise_subscribe_args(args, kwargs)
        stop_event = threading.Event()
        ready_event = threading.Event()
        socket_ref: dict[str, socket.socket] = {}
        thread = threading.Thread(
            target=_subscription_worker,
            args=(socket_path, listener, session_id, after_cursor, stop_event, ready_event, socket_ref),
            daemon=True,
        )
        thread.start()
        ready_event.wait(timeout=1)

        def unsubscribe() -> None:
            stop_event.set()
            sock = socket_ref.get("socket")
            if sock:
                with contextlib.suppress(Exception):
                    sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(Exception):
                    sock.close()
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(timeout=1)

        return unsubscribe

    async def close() -> None:
        return None

    return {
        "_daemonClient": True,
        "_socketPath": socket_path,
        "_request": request,
        "close": close,
        "addAttachment": lambda _session_id=None: (lambda: None),
        "getBootstrap": get_bootstrap,
        "getStatus": lambda: request("getStatus", {}),
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
        "getState": lambda: {"remote": True, "socketPath": socket_path},
    }


async def _handle_daemon_request(supervisor: dict[str, Any], shutdown: asyncio.Event, request: dict[str, Any]) -> dict[str, Any]:
    request_id = None
    try:
        request_id = request.get("id")
        method = _normalise_daemon_method(request.get("method"))
        params = request.get("params") or {}
        result: Any
        if method == "getState":
            result = supervisor["getState"]()
        elif method == "getStatus":
            result = await loadDaemonState()
        elif method == "getBootstrap":
            result = await supervisor["getBootstrap"](params.get("sessionId"))
        elif method == "listTasks":
            result = await supervisor["listTasks"](params.get("sessionId"))
        elif method == "inspectTask":
            result = await supervisor["inspectTask"](params.get("taskId"))
        elif method == "readTaskOutput":
            result = await supervisor["readTaskOutput"](params.get("taskId"), params.get("tailBytes"))
        elif method == "stopTask":
            result = await supervisor["stopTask"](params.get("taskId"))
        elif method == "retryTask":
            result = await supervisor["retryTask"](params.get("taskId"))
        elif method == "startBackgroundCommand":
            result = await supervisor["startBackgroundCommand"](params)
        elif method == "startMainSessionTask":
            result = await supervisor["startMainSessionTask"](params)
        elif method == "startSubagentTask":
            result = await supervisor["startSubagentTask"](params)
        elif method == "startHealthCheck":
            result = await supervisor["startHealthCheck"](params)
        elif method == "shutdown":
            result = {"status": "shutting_down"}
            shutdown.set()
            return {"id": request_id, "ok": True, "result": result, "shutdown": True}
        else:
            raise RuntimeError(f"Unknown daemon method: {method}")
        return {"id": request_id, "ok": True, "result": result}
    except Exception as exc:
        return {"id": request_id, "ok": False, "error": str(exc)}


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


def _subscription_worker(
    socket_path: str,
    listener: Callable[[dict[str, Any]], Any],
    session_id: str | None,
    after_cursor: int | None,
    stop_event: threading.Event,
    ready_event: threading.Event,
    socket_ref: dict[str, socket.socket],
) -> None:
    buffer = b""
    saw_ack = False
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_ref["socket"] = sock
        sock.connect(socket_path)
        sock.settimeout(0.2)
        params: dict[str, Any] = {}
        if session_id:
            params["sessionId"] = session_id
        if isinstance(after_cursor, int):
            params["afterCursor"] = after_cursor
        request = {"id": str(uuid.uuid4()), "method": "subscribe", "params": params}
        sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        while not stop_event.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line:
                    continue
                payload = json.loads(line.decode("utf-8"))
                if not saw_ack and payload.get("ok") is True and "result" in payload:
                    saw_ack = True
                    ready_event.set()
                    continue
                saw_ack = True
                ready_event.set()
                record = payload.get("record") if payload.get("type") == "event" and isinstance(payload.get("record"), dict) else payload
                _invoke_subscription_listener(listener, record)
    except Exception:
        ready_event.set()
    finally:
        ready_event.set()
        if sock:
            with contextlib.suppress(Exception):
                sock.close()
        socket_ref.pop("socket", None)


def _invoke_subscription_listener(listener: Callable[[dict[str, Any]], Any], payload: dict[str, Any]) -> None:
    try:
        result = listener(payload)
        if hasattr(result, "__await__"):
            asyncio.run(result)
    except Exception:
        pass


def _subscription_session_id(value: str | dict[str, Any] | None) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        maybe = value.get("sessionId") or value.get("session_id")
        return maybe if isinstance(maybe, str) and maybe else None
    return None


def _normalise_subscribe_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Callable[[dict[str, Any]], Any], str | None, int | None]:
    listener: Any = kwargs.get("listener")
    session_ref: Any = kwargs.get("sessionId") or kwargs.get("session_id")
    if args and callable(args[0]):
        listener = args[0]
        if len(args) > 1:
            session_ref = args[1]
    elif len(args) >= 2 and callable(args[1]):
        session_ref = args[0]
        listener = args[1]
    if not callable(listener):
        raise TypeError("subscribe requires a listener callback")
    after_cursor = kwargs.get("afterCursor") or kwargs.get("after_cursor")
    if isinstance(session_ref, dict):
        after_cursor = session_ref.get("afterCursor") or session_ref.get("after_cursor") or after_cursor
    return listener, _subscription_session_id(session_ref), int(after_cursor) if isinstance(after_cursor, (int, float, str)) and str(after_cursor).isdigit() else None


def _event_matches_session(payload: dict[str, Any], session_id: str | None) -> bool:
    if not session_id:
        return True
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("sessionId") == session_id or data.get("sessionId") == session_id or data.get("session_id") == session_id


def _normalise_daemon_method(method: Any) -> str | None:
    aliases = {
        "daemon.status": "getState",
        "daemon.getStatus": "getStatus",
        "daemon.getState": "getState",
        "daemon.getBootstrap": "getBootstrap",
        "daemon.listTasks": "listTasks",
        "daemon.inspectTask": "inspectTask",
        "daemon.readTaskOutput": "readTaskOutput",
        "daemon.stopTask": "stopTask",
        "daemon.retryTask": "retryTask",
        "daemon.startBackgroundCommand": "startBackgroundCommand",
        "daemon.startMainSessionTask": "startMainSessionTask",
        "daemon.startSubagentTask": "startSubagentTask",
        "daemon.startHealthCheck": "startHealthCheck",
        "daemon.subscribe": "subscribe",
        "daemon.shutdown": "shutdown",
        "tasks.list": "listTasks",
        "tasks.inspect": "inspectTask",
        "tasks.read_output": "readTaskOutput",
        "tasks.stop": "stopTask",
        "tasks.retry": "retryTask",
        "tasks.start_background_command": "startBackgroundCommand",
        "tasks.start_main_session": "startMainSessionTask",
        "tasks.start_subagent": "startSubagentTask",
        "tasks.start_health_check": "startHealthCheck",
    }
    return aliases.get(method, method if isinstance(method, str) else None)


def _last_daemon_event_cursor(events: list[dict[str, Any]]) -> int:
    cursors = [event.get("cursor") for event in events if isinstance(event.get("cursor"), int)]
    return max(cursors, default=0)


async def _record_daemon_lifecycle_event(event: str, data: dict[str, Any], next_cursor: Callable[[], int]) -> None:
    await appendDaemonEvent({"cursor": next_cursor(), "createdAt": now_iso(), "event": event, "data": data})


async def _cleanup_stale_daemon_artifacts() -> None:
    state = await loadDaemonState()
    if state:
        pid = state.get("pid")
        if not isinstance(pid, int) or not _is_process_alive(pid):
            await clearDaemonState()
            return
    socket_path = daemonSocketPath()
    if os.path.exists(socket_path) and not state:
        with contextlib.suppress(FileNotFoundError):
            os.remove(socket_path)


async def _wait_for_process_exit(pid: int, timeout_seconds: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if not _is_process_alive(pid):
            return
        await asyncio.sleep(0.05)


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
