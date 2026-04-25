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
    loadDaemonState,
    saveDaemonState,
)
from .task_supervisor import createTaskSupervisor


_SUPERVISOR: dict[str, Any] | None = None
_DAEMON_PROCESS: subprocess.Popen[Any] | None = None


async def runTaskSupervisorDaemon(cwd: str) -> None:
    await ensureBaseLayout(cwd)
    socket_path = daemonSocketPath()
    try:
        os.remove(socket_path)
    except FileNotFoundError:
        pass

    subscribers: dict[str, dict[str, Any]] = {}

    async def on_event(event: dict[str, Any]) -> None:
        payload = {"createdAt": now_iso(), **event}
        await appendDaemonEvent(payload)
        await _publish_daemon_event(subscribers, payload)

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
                    subscribers[subscriber_id] = {
                        "writer": writer,
                        "sessionId": params.get("sessionId") or params.get("session_id"),
                    }
                    writer.write(
                        json.dumps({"id": request.get("id"), "ok": True, "result": {"subscribed": True}}, ensure_ascii=False).encode("utf-8")
                        + b"\n"
                    )
                    await writer.drain()
                    await _wait_for_subscription_close(reader, shutdown)
                    break

                response = await _handle_daemon_request(supervisor, shutdown, request)
                writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
                await writer.drain()
                if response.get("shutdown"):
                    break
        finally:
            if subscriber_id:
                subscribers.pop(subscriber_id, None)
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle_client, path=socket_path)
    await saveDaemonState(
        {
            "pid": os.getpid(),
            "cwd": cwd,
            "socketPath": socket_path,
            "startedAt": now_iso(),
            "mode": "detached_python",
            "status": "running",
        }
    )
    try:
        async with server:
            await shutdown.wait()
    finally:
        server.close()
        await server.wait_closed()
        for subscriber in list(subscribers.values()):
            writer = subscriber.get("writer")
            if writer:
                writer.close()
        await supervisor["close"]()
        await clearDaemonState()


async def connectTaskSupervisorDaemonClient(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
    args = {**(args or {}), **kwargs}
    socket_path = args.get("socketPath") or daemonSocketPath()
    if not os.path.exists(socket_path):
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

    def subscribe(listener: Callable[[dict[str, Any]], Any], sessionId: str | dict[str, Any] | None = None):
        session_id = _subscription_session_id(sessionId)
        stop_event = threading.Event()
        ready_event = threading.Event()
        socket_ref: dict[str, socket.socket] = {}
        thread = threading.Thread(
            target=_subscription_worker,
            args=(socket_path, listener, session_id, stop_event, ready_event, socket_ref),
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
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    stale: list[str] = []
    for subscriber_id, subscriber in list(subscribers.items()):
        if not _event_matches_session(payload, subscriber.get("sessionId")):
            continue
        writer = subscriber.get("writer")
        if not writer:
            stale.append(subscriber_id)
            continue
        try:
            writer.write(encoded)
            await writer.drain()
        except Exception:
            stale.append(subscriber_id)
    for subscriber_id in stale:
        subscriber = subscribers.pop(subscriber_id, None)
        writer = subscriber.get("writer") if subscriber else None
        if writer:
            writer.close()


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
        request = {"id": str(uuid.uuid4()), "method": "subscribe", "params": {"sessionId": session_id} if session_id else {}}
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
                _invoke_subscription_listener(listener, payload)
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


def _event_matches_session(payload: dict[str, Any], session_id: str | None) -> bool:
    if not session_id:
        return True
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("sessionId") == session_id or data.get("sessionId") == session_id or data.get("session_id") == session_id


def _normalise_daemon_method(method: Any) -> str | None:
    aliases = {
        "daemon.status": "getState",
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
    }
    return aliases.get(method, method if isinstance(method, str) else None)


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
