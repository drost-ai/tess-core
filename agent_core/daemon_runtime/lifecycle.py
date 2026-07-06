from __future__ import annotations

from .shared import *

_SUPERVISOR: dict[str, Any] | None = None
_DAEMON_PROCESS: subprocess.Popen[Any] | None = None

async def connectTaskSupervisorDaemonClient(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
    args = {**(args or {}), **kwargs}
    state = await loadDaemonState()
    socket_path = args.get("socketPath") or (state or {}).get("socketPath") or daemonSocketPath()
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

    state = await loadDaemonState()
    pid = state.get("pid") if state else None
    if isinstance(pid, int) and _is_process_alive(pid) and _daemon_state_is_wedged(state):
        await _terminate_wedged_daemon(pid)
        await clearDaemonState()

    await _spawn_detached_daemon(cwd)
    deadline = asyncio.get_running_loop().time() + _resolve_startup_timeout_seconds(args)
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        if _DAEMON_PROCESS and _DAEMON_PROCESS.poll() is not None:
            code = _DAEMON_PROCESS.returncode
            message = f"Task supervisor daemon exited during startup with code {code}; see {os.path.join(daemonLogsDir(), 'bootstrap.log')}"
            if args.get("allowInProcessFallback", True):
                await _record_daemon_unavailable("spawn_exited", message)
                return await _ensure_in_process_supervisor(cwd)
            raise RuntimeError(message)
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
    await saveDaemonState({"pid": os.getpid(), "cwd": cwd, "startedAt": now_iso(), "mode": "in_process_fallback", "status": "degraded", "daemonUnavailable": {"mode": "in_process_fallback", "reason": "detached_daemon_unavailable"}})
    await appendDaemonEvent({"createdAt": now_iso(), "event": "daemon.unavailable", "data": {"mode": "in_process_fallback", "reason": "detached_daemon_unavailable"}})
    return _SUPERVISOR

async def _spawn_detached_daemon(cwd: str) -> None:
    global _DAEMON_PROCESS
    await ensureBaseLayout(cwd)
    os.makedirs(daemonLogsDir(), exist_ok=True)
    log_path = os.path.join(daemonLogsDir(), "bootstrap.log")
    env = dict(os.environ)
    env["AGENT_CORE_HOME"] = getStateRoot()
    repo_root = str(Path(__file__).resolve().parents[2])
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

async def _cleanup_stale_daemon_artifacts() -> None:
    state = await loadDaemonState()
    if state:
        pid = state.get("pid")
        if not isinstance(pid, int) or not _is_process_alive(pid):
            await clearDaemonState()
            state = None
    await _reap_daemon_tmp_artifacts()
    socket_path = daemonSocketPath()
    if os.path.exists(socket_path) and not state:
        with contextlib.suppress(FileNotFoundError):
            os.remove(socket_path)

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

def _resolve_startup_timeout_seconds(args: dict[str, Any]) -> float:
    env_value = os.environ.get("AGENT_CORE_DAEMON_START_TIMEOUT_MS")
    try:
        env_ms = float(env_value) if env_value else 0
    except ValueError:
        env_ms = 0
    if env_ms > 0:
        return env_ms / 1000
    if args.get("startupTimeoutSeconds") is not None:
        return float(args.get("startupTimeoutSeconds") or 0)
    if args.get("startTimeoutMs") is not None:
        return float(args.get("startTimeoutMs") or 0) / 1000
    return 30.0

def _daemon_state_is_wedged(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    phase = state.get("phase")
    started_at = state.get("startedAt")
    try:
        import datetime

        started = datetime.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        age_seconds = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
    except Exception:
        age_seconds = 60 * 60
    return phase == "running" or age_seconds > 5 * 60

async def _terminate_wedged_daemon(pid: int) -> None:
    if not await _is_daemon_run_process(pid):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    await _wait_for_process_exit(pid, 2)
    if _is_process_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)

async def _is_daemon_run_process(pid: int) -> bool:
    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    try:
        result = await asyncio.to_thread(run)
    except Exception:
        return False
    command = result.stdout.strip()
    return result.returncode == 0 and "agent_core.daemon_worker" in command

async def _record_daemon_unavailable(reason: str, detail: str) -> None:
    await appendDaemonEvent({"createdAt": now_iso(), "event": "daemon.unavailable", "data": {"mode": "in_process_fallback", "reason": reason, "detail": detail}})

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
