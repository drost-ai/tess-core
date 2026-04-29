from __future__ import annotations

from .shared import *

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
