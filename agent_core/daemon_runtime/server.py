from __future__ import annotations

from .shared import *

async def runTaskSupervisorDaemon(cwd: str) -> None:
    await ensureBaseLayout(cwd)
    socket_path = daemonSocketPath()
    await _cleanup_stale_daemon_artifacts()
    await rotateDaemonEventsIfOversized()
    try:
        os.remove(socket_path)
    except FileNotFoundError:
        pass

    subscribers: dict[str, dict[str, Any]] = {}
    cursor = await readLastDaemonEventCursor()
    daemon_epoch = str(uuid.uuid4())
    daemon_started_at = now_iso()
    daemon_phase = "recovering"
    supervisor: dict[str, Any] | None = None
    rotation_task: asyncio.Task[Any] | None = None

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
    rotation_task = asyncio.create_task(_rotate_daemon_events_periodically(shutdown))

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
        if rotation_task:
            rotation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rotation_task
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


async def _rotate_daemon_events_periodically(shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=5 * 60)
        except asyncio.TimeoutError:
            await rotateDaemonEventsIfOversized()
