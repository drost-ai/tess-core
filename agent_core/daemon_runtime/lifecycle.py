from __future__ import annotations

from .shared import *
from .._utils import maybe_await
from .client import _normalise_subscribe_args
from .events import (
    _collaboration_snapshot_payload,
    _event_matches_session,
    _make_snapshot_record,
    _monitor_snapshot_payload,
    _task_snapshot_payload,
)


_SUPERVISOR: dict[str, Any] | None = None
_DAEMON_PROCESS: subprocess.Popen[Any] | None = None
_DAEMON_BOOT_GRACE_SECONDS = 5 * 60


async def connectTaskSupervisorDaemonClient(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    values = {**(args or {}), **kwargs}
    state = await loadDaemonState()
    if not state:
        return _local_supervisor_fallback()
    pid = state.get("pid")
    if not isinstance(pid, int) or not _is_process_alive(pid):
        await _cleanup_stale_daemon_artifacts()
        return None
    if state.get("phase") != "running":
        return None
    socket_path = values.get("socketPath") or state.get("socketPath")
    if socket_path != daemonSocketPath():
        raise DaemonProtocolError(
            "daemon.auth_failed",
            "Daemon state names an unexpected socket path.",
        )
    await verifyPrivateDaemonRuntime()
    auth = await readDaemonAuthMaterial(state)
    client = _create_daemon_client(socket_path, state, auth)
    try:
        await client["_request"]("getState", {})
        return client
    except (DaemonProtocolError, DaemonSecurityError):
        await client["close"]()
        raise
    except (ConnectionError, FileNotFoundError, OSError, RuntimeError):
        await client["close"]()
        return None


async def ensureTaskSupervisorDaemon(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    global _SUPERVISOR
    values = {**(args or {}), **kwargs}
    cwd = values.get("cwd") or os.getcwd()
    if values.get("inProcess"):
        return await _ensure_in_process_supervisor(cwd)
    await prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True})
    await ensureBaseLayout(cwd)
    budget = _resolve_startup_timeout_seconds(values)
    async with withRecordTransaction(
        "daemon-ensure",
        getStateRoot(),
        timeoutMs=int((budget + 5) * 1000),
    ):
        client = await _ensure_daemon_under_lock(cwd, values, budget)
    _SUPERVISOR = client
    return client


async def _ensure_daemon_under_lock(
    cwd: str,
    args: dict[str, Any],
    budget: float,
) -> dict[str, Any]:
    existing_client = await connectTaskSupervisorDaemonClient()
    if existing_client is not None:
        return existing_client
    state = await loadDaemonState()
    pid = (state or {}).get("pid")
    if isinstance(pid, int) and _is_process_alive(pid):
        if _daemon_state_is_still_booting(state):
            client = await _wait_for_running_daemon(pid, budget)
            if client is not None:
                return client
            if _is_process_alive(pid):
                raise RuntimeError(
                    f"Task supervisor daemon (pid {pid}) is still starting — phase {state.get('phase')}."
                )
        else:
            terminated = await _terminate_wedged_daemon(pid)
            if not terminated and _is_process_alive(pid):
                raise RuntimeError(
                    f"Refusing to replace live pid {pid}: it could not be verified as this runtime's daemon."
                )
    await _cleanup_stale_daemon_artifacts()
    last_error: BaseException | None = None
    for attempt in (1, 2):
        process = await _spawn_detached_daemon(cwd)
        deadline = asyncio.get_running_loop().time() + budget
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            if process.poll() is not None:
                last_error = RuntimeError(
                    f"Task supervisor daemon exited during startup with code {process.returncode}."
                )
                break
            client = await connectTaskSupervisorDaemonClient()
            if client is not None:
                return client
        if process.poll() is None:
            last_error = RuntimeError("Timed out waiting for task supervisor daemon to start.")
            break
        if attempt == 1:
            await _cleanup_stale_daemon_artifacts()
    message = (
        f"Unable to start task supervisor daemon: {last_error}; "
        f"see {os.path.join(daemonLogsDir(), 'bootstrap.log')}"
    )
    if args.get("allowInProcessFallback", True):
        await _record_daemon_unavailable("spawn_failed", message)
        return await _ensure_in_process_supervisor(cwd)
    raise RuntimeError(message)


async def stopTaskSupervisorDaemon() -> None:
    global _SUPERVISOR, _DAEMON_PROCESS
    client = (
        _SUPERVISOR
        if (
            _SUPERVISOR
            and _SUPERVISOR.get("_daemonClient")
            and not _SUPERVISOR.get("_isClosed", lambda: False)()
        )
        else await connectTaskSupervisorDaemonClient()
    )
    if client and client.get("_daemonClient"):
        with contextlib.suppress(Exception):
            await client["_request"]("shutdown", {})
        await client["close"]()
    elif _SUPERVISOR:
        await _SUPERVISOR["close"]()
    state = await loadDaemonState()
    pid = (state or {}).get("pid")
    if isinstance(pid, int) and pid != os.getpid():
        await _wait_for_process_exit(pid, 2)
        if _is_process_alive(pid) and await _is_daemon_run_process(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
            await _wait_for_process_exit(pid, 2)
    if _DAEMON_PROCESS and _DAEMON_PROCESS.poll() is None:
        _DAEMON_PROCESS.terminate()
        try:
            _DAEMON_PROCESS.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _DAEMON_PROCESS.kill()
    _SUPERVISOR = None
    _DAEMON_PROCESS = None
    await _cleanup_stale_daemon_artifacts()


async def getTaskSupervisorDaemonState() -> dict[str, Any] | None:
    state = await loadDaemonState()
    local = _local_supervisor_fallback()
    if local is not None:
        local_state = local["getState"]()
        return {**(state or {}), **local_state, "supervisor": local_state}
    client = await connectTaskSupervisorDaemonClient()
    if state and client:
        with contextlib.suppress(Exception):
            state = {**state, "supervisor": await client["_request"]("getState", {})}
        if client.get("_daemonClient"):
            await client["close"]()
    return state


async def _ensure_in_process_supervisor(cwd: str) -> dict[str, Any]:
    global _SUPERVISOR
    if _SUPERVISOR is not None and not _SUPERVISOR.get("_daemonClient"):
        return _SUPERVISOR

    async def on_event(event: dict[str, Any]) -> None:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        await appendDaemonEvent(
            {
                "createdAt": now_iso(),
                "taskId": data.get("task_id") if isinstance(data.get("task_id"), str) else None,
                **event,
            }
        )

    _SUPERVISOR = await createTaskSupervisor({"cwd": cwd, "onEvent": on_event})
    _install_in_process_subscription_parity(_SUPERVISOR)
    await saveDaemonState(
        {
            "pid": os.getpid(),
            "cwd": cwd,
            "startedAt": now_iso(),
            "mode": "in_process_fallback",
            "status": "degraded",
            "daemonUnavailable": {
                "mode": "in_process_fallback",
                "reason": "detached_daemon_unavailable",
            },
        }
    )
    await appendDaemonEvent(
        {
            "createdAt": now_iso(),
            "event": "daemon.unavailable",
            "data": {
                "mode": "in_process_fallback",
                "reason": "detached_daemon_unavailable",
            },
        }
    )
    return _SUPERVISOR


def _install_in_process_subscription_parity(supervisor: dict[str, Any]) -> None:
    live_subscribe = supervisor["subscribe"]

    def subscribe(*args: Any, **kwargs: Any):
        listener, session_id, after_cursor = _normalise_subscribe_args(args, kwargs)
        unsubscribe_live = live_subscribe(listener, session_id)
        release_attachment = supervisor["addAttachment"](session_id)
        replay_task: asyncio.Task[Any] | None = None

        async def replay() -> None:
            records = await loadDaemonEvents()
            for record in records:
                cursor = record.get("cursor")
                if isinstance(cursor, int) and cursor <= (after_cursor or 0):
                    continue
                if _event_matches_session(record, session_id):
                    await maybe_await(listener(record))
            if not session_id:
                return
            bootstrap = await supervisor["getBootstrap"](session_id)
            next_cursor = max(
                [record.get("cursor", 0) for record in records if isinstance(record.get("cursor"), int)],
                default=0,
            )
            snapshots = (
                ("task.snapshot", _task_snapshot_payload(session_id, bootstrap.get("taskSnapshot") or {})),
                ("monitor.snapshot", _monitor_snapshot_payload(session_id, bootstrap.get("monitorSnapshot") or {})),
                ("collaboration.snapshot", _collaboration_snapshot_payload(bootstrap.get("collaborationSnapshot") or {})),
            )
            for event_name, data in snapshots:
                next_cursor += 1
                await maybe_await(
                    listener(
                        _make_snapshot_record(
                            next_cursor,
                            session_id,
                            event_name,
                            data,
                        )
                    )
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(replay())
        else:
            replay_task = loop.create_task(replay())
        unsubscribed = False

        def unsubscribe() -> None:
            nonlocal unsubscribed
            if unsubscribed:
                return
            unsubscribed = True
            if replay_task is not None and not replay_task.done():
                replay_task.cancel()
            unsubscribe_live()
            release_attachment()

        return unsubscribe

    supervisor["subscribe"] = subscribe


async def _spawn_detached_daemon(cwd: str) -> subprocess.Popen[Any]:
    global _DAEMON_PROCESS
    await ensureBaseLayout(cwd)
    os.makedirs(daemonLogsDir(), mode=0o700, exist_ok=True)
    os.chmod(daemonLogsDir(), 0o700)
    log_path = os.path.join(daemonLogsDir(), "bootstrap.log")
    env = dict(os.environ)
    env["AGENT_CORE_HOME"] = getStateRoot()
    repo_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = repo_root if not env.get("PYTHONPATH") else f"{repo_root}{os.pathsep}{env['PYTHONPATH']}"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    try:
        _DAEMON_PROCESS = subprocess.Popen(
            [sys.executable, "-m", "agent_core.daemon_worker", "--cwd", cwd],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        os.close(fd)
    await asyncio.to_thread(_append_spawn_ledger, _DAEMON_PROCESS.pid)
    return _DAEMON_PROCESS


async def _cleanup_stale_daemon_artifacts() -> None:
    async with withRecordTransaction(
        "daemon-instance",
        getStateRoot(),
        timeoutMs=_daemon_instance_wait_ms(),
    ):
        state = await loadDaemonState()
        if state and isinstance(state.get("pid"), int) and _is_process_alive(state["pid"]):
            return
        current = await loadDaemonState()
        if state != current:
            return
        socket_path = daemonSocketPath()
        with contextlib.suppress(FileNotFoundError):
            await removeInactiveOwnedDaemonSocket(socket_path)
        token_id = (state or {}).get("authTokenId")
        if isinstance(token_id, str):
            await removeDaemonAuthMaterialIfOwned(token_id)
        else:
            await removeOwnedDaemonAuthMaterial()
        await clearDaemonState()
        await _reap_daemon_tmp_artifacts()


async def _reap_daemon_tmp_artifacts() -> None:
    try:
        names = os.listdir(getDaemonDir())
    except FileNotFoundError:
        return
    cutoff = time.time() - 60 * 60
    for name in names:
        if not re.match(r"^state\.json\..+\.tmp$", name):
            continue
        path = os.path.join(getDaemonDir(), name)
        with contextlib.suppress(OSError):
            if os.path.getmtime(path) < cutoff:
                os.remove(path)


async def _wait_for_running_daemon(pid: int, timeout: float) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not _is_process_alive(pid):
            return None
        client = await connectTaskSupervisorDaemonClient()
        if client is not None:
            return client
        await asyncio.sleep(0.05)
    return None


def _resolve_startup_timeout_seconds(args: dict[str, Any]) -> float:
    env_value = os.environ.get("AGENT_CORE_DAEMON_START_TIMEOUT_MS")
    try:
        env_ms = float(env_value) if env_value else 0
    except ValueError:
        env_ms = 0
    if env_ms > 0:
        return env_ms / 1000
    if args.get("startupTimeoutSeconds") is not None:
        return max(0.025, float(args.get("startupTimeoutSeconds") or 0))
    if args.get("startTimeoutMs") is not None:
        return max(0.025, float(args.get("startTimeoutMs") or 0) / 1000)
    return 30.0


def _daemon_state_is_still_booting(state: dict[str, Any] | None) -> bool:
    if not state or state.get("phase") == "running":
        return False
    try:
        import datetime

        started = datetime.datetime.fromisoformat(str(state.get("startedAt")).replace("Z", "+00:00"))
        age = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
    except Exception:
        age = _DAEMON_BOOT_GRACE_SECONDS + 1
    return age < _DAEMON_BOOT_GRACE_SECONDS


async def _terminate_wedged_daemon(pid: int) -> bool:
    if not await _is_daemon_run_process(pid):
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    await _wait_for_process_exit(pid, 2)
    if _is_process_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    return not _is_process_alive(pid)


async def _is_daemon_run_process(pid: int) -> bool:
    def inspect() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    try:
        result = await asyncio.to_thread(inspect)
    except Exception:
        return False
    return result.returncode == 0 and "agent_core.daemon_worker" in result.stdout


async def _record_daemon_unavailable(reason: str, detail: str) -> None:
    await appendDaemonEvent(
        {
            "createdAt": now_iso(),
            "event": "daemon.unavailable",
            "data": {"mode": "in_process_fallback", "reason": reason, "detail": detail},
        }
    )


async def _wait_for_process_exit(pid: int, timeout_seconds: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if not _is_process_alive(pid):
            return
        await asyncio.sleep(0.05)


def _append_spawn_ledger(pid: int) -> None:
    path = os.path.join(getDaemonDir(), "spawn-ledger.jsonl")
    payload = json.dumps({"pid": pid, "createdAt": now_iso()}, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        raw = memoryview(payload.encode("utf-8"))
        while raw:
            written = os.write(fd, raw)
            if written <= 0:
                raise OSError("Daemon spawn-ledger write made no progress.")
            raw = raw[written:]
    finally:
        os.close(fd)


def _local_supervisor_fallback() -> dict[str, Any] | None:
    return _SUPERVISOR if _SUPERVISOR and not _SUPERVISOR.get("_daemonClient") else None


def _is_process_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
