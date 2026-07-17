from __future__ import annotations

from .shared import *


async def runTaskSupervisorDaemon(cwd: str) -> None:
    await prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True})
    wait_ms = _daemon_instance_wait_ms()
    async with withRecordTransaction(
        "daemon-instance",
        getStateRoot(),
        timeoutMs=wait_ms,
    ):
        await _run_task_supervisor_daemon_scoped(cwd)


async def _run_task_supervisor_daemon_scoped(cwd: str) -> None:
    await ensureBaseLayout(cwd)
    socket_path = daemonSocketPath()
    existing = await loadDaemonState()
    existing_pid = (existing or {}).get("pid")
    if isinstance(existing_pid, int) and _is_process_alive(existing_pid):
        raise RuntimeError(f"Daemon already running with pid {existing_pid}")
    await _cleanup_stale_daemon_artifacts()
    await rotateDaemonEventsIfOversized()
    cursor = max(await readLastDaemonEventCursor(), int((existing or {}).get("cursor") or 0))
    daemon_epoch = str(uuid.uuid4())
    daemon_started_at = now_iso()
    daemon_phase = "starting"
    auth = await createDaemonAuthMaterial()
    subscribers: dict[str, dict[str, Any]] = {}
    shutdown = asyncio.Event()
    supervisor: dict[str, Any] | None = None
    transport: AuthenticatedDaemonServer | None = None
    rotation_task: asyncio.Task[Any] | None = None
    state_lock = asyncio.Lock()

    def next_event_cursor() -> int:
        nonlocal cursor
        cursor += 1
        return cursor

    async def write_daemon_state(phase: str | None = None) -> None:
        nonlocal daemon_phase
        if phase:
            daemon_phase = phase
        async with state_lock:
            supervisor_state = supervisor["getState"]() if supervisor else {}
            await saveDaemonState(
                {
                    "instanceId": "local-daemon",
                    "protocolVersion": DAEMON_PROTOCOL_VERSION,
                    "authTokenId": auth["tokenId"],
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
                    "cursor": cursor,
                }
            )

    async def on_event(event: dict[str, Any]) -> None:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        payload = {
            "cursor": next_event_cursor(),
            "createdAt": now_iso(),
            "taskId": data.get("task_id") if isinstance(data.get("task_id"), str) else None,
            **event,
        }
        await appendDaemonEvent(payload)
        await _publish_daemon_event(subscribers, payload)
        await write_daemon_state()

    async def on_authenticated(_peer: DaemonServerPeer) -> None:
        await write_daemon_state()

    async def on_disconnect(peer: DaemonServerPeer) -> None:
        subscriber = subscribers.pop(peer.id, None)
        release = subscriber.get("releaseAttachment") if subscriber else None
        if callable(release):
            release()
        if daemon_phase != "shutting_down":
            await write_daemon_state()

    async def dispatch(
        peer: DaemonServerPeer,
        request: dict[str, Any],
        signal: DaemonAbortSignal,
    ) -> dict[str, Any]:
        if _normalise_daemon_method(request.get("method")) != "subscribe":
            return await _handle_daemon_request(supervisor or {}, shutdown, request, signal)
        params = request.get("params") or {}
        session_id = params.get("sessionId") or params.get("session_id")
        previous = subscribers.pop(peer.id, None)
        previous_release = previous.get("releaseAttachment") if previous else None
        if callable(previous_release):
            previous_release()
        release_attachment = supervisor["addAttachment"](session_id) if supervisor else lambda: None
        subscribers[peer.id] = {
            "peer": peer,
            "sessionId": session_id,
            "releaseAttachment": release_attachment,
        }
        after_cursor = int(params.get("afterCursor") or params.get("after_cursor") or 0)
        await write_daemon_state()

        async def after_write() -> None:
            await _replay_daemon_events(peer, session_id, after_cursor)
            if supervisor:
                await _send_bootstrap_snapshots(
                    peer,
                    supervisor,
                    session_id,
                    next_event_cursor,
                )

        return {"result": {"subscribed": True}, "afterWrite": after_write}

    loop = asyncio.get_running_loop()
    installed_signals: list[int] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, shutdown.set)
            installed_signals.append(signum)
    try:
        await write_daemon_state("starting")
        supervisor = await createTaskSupervisor({"cwd": cwd, "onEvent": on_event})
        rotation_task = asyncio.create_task(_rotate_daemon_events_periodically(shutdown))
        transport = createAuthenticatedDaemonServer(
            token=auth["token"],
            epoch=daemon_epoch,
            dispatch=dispatch,
            onAuthenticated=on_authenticated,
            onDisconnect=on_disconnect,
        )
        _inject_daemon_boot_fault("pre_bind")
        await transport.listen(socket_path)
        os.chmod(socket_path, 0o600)
        verifyOwnedDaemonSocket(socket_path)
        _inject_daemon_boot_fault("post_bind")
        await _record_daemon_lifecycle_event(
            "daemon.recovering",
            {"phase": "recovering", "epoch": daemon_epoch},
            next_event_cursor,
        )
        await write_daemon_state("recovering")
        _inject_daemon_boot_fault("pre_ready")
        await write_daemon_state("running")
        await _record_daemon_lifecycle_event(
            "daemon.recovered",
            {"phase": "running", "epoch": daemon_epoch},
            next_event_cursor,
        )
        await shutdown.wait()
    finally:
        daemon_phase = "shutting_down"
        if rotation_task:
            rotation_task.cancel()
            await asyncio.gather(rotation_task, return_exceptions=True)
        if transport:
            with contextlib.suppress(Exception):
                await write_daemon_state("shutting_down")
            await transport.close()
        for subscriber in list(subscribers.values()):
            release = subscriber.get("releaseAttachment")
            if callable(release):
                release()
        subscribers.clear()
        if supervisor:
            await supervisor["close"]()
        await _clear_owned_daemon_instance(
            daemon_epoch,
            os.getpid(),
            auth["tokenId"],
            socket_path,
        )
        for signum in installed_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)


async def _clear_owned_daemon_instance(
    epoch: str,
    pid: int,
    token_id: str,
    socket_path: str,
) -> None:
    state = await loadDaemonState()
    if state and (state.get("epoch") != epoch or state.get("pid") != pid):
        return
    socket_info = verifyOwnedDaemonSocket(socket_path)
    for target in (daemonPidPath(), daemonStatePath()):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(target)
    await removeDaemonAuthMaterialIfOwned(token_id)
    if socket_info:
        current = verifyOwnedDaemonSocket(socket_path)
        if current and (
            current.st_dev != socket_info.st_dev or current.st_ino != socket_info.st_ino
        ):
            raise DaemonSecurityError(
                "daemon.security_unsafe",
                "Daemon socket identity changed during owned cleanup.",
                socket_path,
            )
        if current:
            os.unlink(socket_path)


async def _rotate_daemon_events_periodically(shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=5 * 60)
        except asyncio.TimeoutError:
            await rotateDaemonEventsIfOversized()


def _daemon_instance_wait_ms() -> int:
    try:
        value = int(os.environ.get("AGENT_CORE_DAEMON_INSTANCE_LOCK_TIMEOUT_MS") or 5_000)
    except ValueError:
        value = 5_000
    return min(30_000, max(100, value))


def _inject_daemon_boot_fault(stage: str) -> None:
    if os.environ.get("AGENT_CORE_TEST_DAEMON_BOOT_FAULT_STAGE") != stage:
        return
    marker = os.environ.get("P02_FAULT_MARKER")
    if marker:
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                raw = memoryview(stage.encode("utf-8"))
                while raw:
                    written = os.write(fd, raw)
                    if written <= 0:
                        raise OSError("P02 fault-marker write made no progress.")
                    raw = raw[written:]
            finally:
                os.close(fd)
        except FileExistsError:
            return
    os._exit(86)
