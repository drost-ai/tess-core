from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import uuid
from typing import Any, Callable

from .protocol import (
    DAEMON_MAX_PENDING_PER_CLIENT,
    DAEMON_MAX_REQUEST_FRAME_BYTES,
    DAEMON_MAX_RESPONSE_FRAME_BYTES,
    DAEMON_PROTOCOL_VERSION,
    DaemonLineDecoder,
    DaemonProtocolError,
    encode_daemon_frame,
    is_daemon_auth_response,
    is_daemon_event_message,
    is_daemon_response,
    parse_daemon_frame,
    resolve_daemon_auth_timeout_ms,
    resolve_daemon_rpc_timeout_ms,
)
from .transport import open_authenticated_daemon_connection, read_one_daemon_frame


def _create_daemon_client(
    socket_path: str,
    state: dict[str, Any],
    auth: dict[str, str],
) -> dict[str, Any]:
    guard = threading.Lock()
    active_writers: dict[str, asyncio.StreamWriter] = {}
    subscriptions: set[Callable[[], None]] = set()
    pending_count = 0
    closed = False

    async def request(method: str, params: dict[str, Any] | None = None) -> Any:
        nonlocal pending_count
        request_id = str(uuid.uuid4())
        with guard:
            if closed:
                raise RuntimeError("Daemon connection is closed")
            if pending_count >= DAEMON_MAX_PENDING_PER_CLIENT:
                raise DaemonProtocolError(
                    "daemon.pending_limit",
                    f"Daemon client already has {DAEMON_MAX_PENDING_PER_CLIENT} pending requests.",
                )
            pending_count += 1
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await open_authenticated_daemon_connection(
                socket_path,
                auth["token"],
                state["epoch"],
            )
            with guard:
                if closed:
                    raise RuntimeError("Daemon connection is closed")
                active_writers[request_id] = writer
            writer.write(
                encode_daemon_frame(
                    {"id": request_id, "method": method, "params": params or {}},
                    DAEMON_MAX_REQUEST_FRAME_BYTES,
                )
            )
            await writer.drain()
            decoder = DaemonLineDecoder(DAEMON_MAX_RESPONSE_FRAME_BYTES)
            timeout_ms = resolve_daemon_rpc_timeout_ms()
            try:
                response = await read_one_daemon_frame(
                    reader,
                    decoder,
                    timeout_ms / 1000,
                )
            except asyncio.TimeoutError as error:
                raise DaemonProtocolError(
                    "daemon.request_timeout",
                    f"Daemon RPC '{method}' timed out after {timeout_ms}ms.",
                ) from error
            if not is_daemon_response(response) or response.get("id") != request_id:
                raise DaemonProtocolError(
                    "daemon.frame_malformed",
                    "Daemon returned an invalid response envelope.",
                )
            if not response["ok"]:
                raise RuntimeError(response.get("error") or "Daemon request failed.")
            return response.get("result")
        finally:
            with guard:
                active_writers.pop(request_id, None)
                pending_count = max(0, pending_count - 1)
            if writer:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def start_background_command(task_args: dict[str, Any] | None = None) -> dict[str, Any]:
        return await request("startBackgroundCommand", dict(task_args or {}))

    async def start_main_session_task(task_args: dict[str, Any] | None = None) -> dict[str, Any]:
        return await request("startMainSessionTask", dict(task_args or {}))

    async def start_subagent_task(task_args: dict[str, Any] | None = None) -> dict[str, Any]:
        return await request("startSubagentTask", dict(task_args or {}))

    async def start_health_check(task_args: dict[str, Any] | None = None) -> dict[str, Any]:
        return await request("startHealthCheck", dict(task_args or {}))

    async def list_tasks(session_id: str | None = None) -> list[dict[str, Any]]:
        return await request("listTasks", {"sessionId": session_id})

    async def stop_session(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, Any]:
        return await request("stopSession", {**(task_args or {}), **task_kwargs})

    async def inspect_task(task_id: str) -> dict[str, Any] | None:
        return await request("inspectTask", {"taskId": task_id})

    async def read_task_output(task_id: str, tailBytes: int | None = None) -> dict[str, Any]:
        return await request("read_task_output", {"taskId": task_id, "tailBytes": tailBytes})

    async def stop_task(task_id: str) -> bool:
        return await request("stopTask", {"taskId": task_id})

    async def retry_task(task_id: str) -> dict[str, Any]:
        return await request("retryTask", {"taskId": task_id})

    async def get_bootstrap(session_id: str) -> dict[str, Any]:
        return await request("getBootstrap", {"sessionId": session_id})

    def is_closed() -> bool:
        with guard:
            return closed

    def subscribe(*args: Any, **kwargs: Any):
        listener, session_id, after_cursor = _normalise_subscribe_args(args, kwargs)
        stop_event = threading.Event()
        ready_event = threading.Event()
        socket_ref: dict[str, socket.socket] = {}
        error_ref: dict[str, BaseException] = {}
        thread = threading.Thread(
            target=_subscription_worker,
            args=(
                socket_path,
                auth["token"],
                state["epoch"],
                listener,
                session_id,
                after_cursor,
                stop_event,
                ready_event,
                socket_ref,
                error_ref,
            ),
            daemon=True,
        )
        thread.start()
        ready = ready_event.wait(timeout=(resolve_daemon_auth_timeout_ms() / 1000) + 1)
        if not ready:
            stop_event.set()
            sock = socket_ref.get("socket")
            if sock:
                with contextlib.suppress(Exception):
                    sock.close()
            thread.join(timeout=1)
            raise DaemonProtocolError(
                "daemon.request_timeout",
                "Daemon subscription acknowledgement timed out.",
            )
        worker_error = error_ref.get("error")
        if worker_error:
            stop_event.set()
            sock = socket_ref.get("socket")
            if sock:
                with contextlib.suppress(Exception):
                    sock.close()
            thread.join(timeout=1)
            raise worker_error
        unsubscribed = False

        def unsubscribe() -> None:
            nonlocal unsubscribed
            if unsubscribed:
                return
            unsubscribed = True
            stop_event.set()
            sock = socket_ref.get("socket")
            if sock:
                with contextlib.suppress(Exception):
                    sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(Exception):
                    sock.close()
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(timeout=1)
            with guard:
                subscriptions.discard(unsubscribe)

        with guard:
            was_closed = closed
            if not was_closed:
                subscriptions.add(unsubscribe)
        if was_closed:
            unsubscribe()
            raise RuntimeError("Daemon connection is closed")
        return unsubscribe

    async def close() -> None:
        nonlocal closed
        with guard:
            if closed:
                return
            closed = True
            unsubscribers = list(subscriptions)
            subscriptions.clear()
            writers = list(active_writers.values())
        for unsubscribe in unsubscribers:
            unsubscribe()
        for writer in writers:
            writer.close()
        for writer in writers:
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    return {
        "_daemonClient": True,
        "_socketPath": socket_path,
        "_isClosed": is_closed,
        "_request": request,
        "close": close,
        "shutdown": lambda: request("shutdown", {}),
        "addAttachment": lambda _session_id=None: (lambda: None),
        "getBootstrap": get_bootstrap,
        "getStatus": lambda: request("getStatus", {}),
        "listTasks": list_tasks,
        "inspectTask": inspect_task,
        "read_task_output": read_task_output,
        "stopTask": stop_task,
        "stopSession": stop_session,
        "retryTask": retry_task,
        "startBackgroundCommand": start_background_command,
        "startMainSessionTask": start_main_session_task,
        "startSubagentTask": start_subagent_task,
        "startHealthCheck": start_health_check,
        "sendTaskMessage": lambda task_args=None, **task_kwargs: request("sendTaskMessage", {**(task_args or {}), **task_kwargs}),
        "answerTaskQuestion": lambda task_args=None, **task_kwargs: request("answerTaskQuestion", {**(task_args or {}), **task_kwargs}),
        "skipTaskQuestion": lambda task_args=None, **task_kwargs: request("skipTaskQuestion", {**(task_args or {}), **task_kwargs}),
        "readTaskQuestions": lambda task_id: request("readTaskQuestions", {"taskId": task_id}),
        "readTaskEvents": lambda task_id: request("readTaskEvents", {"taskId": task_id}),
        "subscribe": subscribe,
        "getState": lambda: {"remote": True, "socketPath": socket_path},
    }


def _subscription_worker(
    socket_path: str,
    token: str,
    epoch: str,
    listener: Callable[[dict[str, Any]], Any],
    session_id: str | None,
    after_cursor: int | None,
    stop_event: threading.Event,
    ready_event: threading.Event,
    socket_ref: dict[str, socket.socket],
    error_ref: dict[str, BaseException],
) -> None:
    decoder = DaemonLineDecoder(DAEMON_MAX_RESPONSE_FRAME_BYTES)
    saw_ack = False
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_ref["socket"] = sock
        sock.settimeout(resolve_daemon_auth_timeout_ms() / 1000)
        sock.connect(socket_path)
        sock.sendall(
            encode_daemon_frame(
                {"type": "auth", "protocolVersion": DAEMON_PROTOCOL_VERSION, "token": token},
                DAEMON_MAX_REQUEST_FRAME_BYTES,
            )
        )
        auth_response = _recv_one_sync(sock, decoder, stop_event)
        if not is_daemon_auth_response(auth_response) or auth_response.get("epoch") != epoch:
            raise DaemonProtocolError("daemon.auth_failed", "Daemon authentication failed.")
        params: dict[str, Any] = {}
        if session_id:
            params["sessionId"] = session_id
        if isinstance(after_cursor, int):
            params["afterCursor"] = after_cursor
        request_id = str(uuid.uuid4())
        sock.sendall(
            encode_daemon_frame(
                {"id": request_id, "method": "subscribe", "params": params},
                DAEMON_MAX_REQUEST_FRAME_BYTES,
            )
        )
        sock.settimeout(0.2)
        while not stop_event.is_set():
            try:
                chunk = sock.recv(64 * 1024)
            except socket.timeout:
                continue
            if not chunk:
                if not saw_ack:
                    raise RuntimeError(
                        "Daemon connection closed before the subscription acknowledgement."
                    )
                break
            for frame in decoder.push(chunk):
                if not frame:
                    continue
                payload = parse_daemon_frame(frame)
                if not saw_ack:
                    if not is_daemon_response(payload) or payload.get("id") != request_id or not payload.get("ok"):
                        raise DaemonProtocolError(
                            "daemon.frame_malformed",
                            "Daemon returned an invalid subscription response.",
                        )
                    saw_ack = True
                    ready_event.set()
                    continue
                if is_daemon_event_message(payload):
                    _invoke_subscription_listener(listener, payload["record"])
    except Exception as error:
        error_ref["error"] = error
        ready_event.set()
    finally:
        ready_event.set()
        if sock:
            with contextlib.suppress(Exception):
                sock.close()
        socket_ref.pop("socket", None)


def _recv_one_sync(
    sock: socket.socket,
    decoder: DaemonLineDecoder,
    stop_event: threading.Event,
) -> Any:
    while not stop_event.is_set():
        chunk = sock.recv(64 * 1024)
        if not chunk:
            raise RuntimeError("Daemon connection closed before a frame arrived.")
        frames = decoder.push(chunk)
        if frames:
            if len(frames) != 1 or decoder.bufferedBytes:
                raise DaemonProtocolError(
                    "daemon.frame_malformed",
                    "Daemon sent data beyond its authentication response.",
                )
            return parse_daemon_frame(frames[0])
    raise RuntimeError("Daemon subscription stopped during authentication.")


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


def _normalise_subscribe_args(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[Callable[[dict[str, Any]], Any], str | None, int | None]:
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
    cursor = int(after_cursor) if isinstance(after_cursor, (int, float, str)) and str(after_cursor).isdigit() else None
    return listener, _subscription_session_id(session_ref), cursor
