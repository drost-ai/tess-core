from __future__ import annotations

from .shared import *

_SUPERVISOR: dict[str, Any] | None = None
_DAEMON_PROCESS: subprocess.Popen[Any] | None = None

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
