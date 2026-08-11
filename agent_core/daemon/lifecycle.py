from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .._utils import now_iso
from ..storage import (
    clear_daemon_state,
    daemon_logs_dir,
    daemon_socket_path,
    daemon_spawn_ledger_path,
    daemon_state_path,
    ensure_base_layout,
    get_state_root,
    load_daemon_state,
    parse_daemon_instance_id,
    with_record_transaction,
)
from .client import _create_daemon_client
from .protocol import DaemonProtocolError
from .security import (
    DaemonSecurityError,
    _is_process_alive,
    prepareDaemonRuntimeSecurity,
    readDaemonAuthMaterial,
    removeDaemonAuthMaterialIfOwned,
    removeInactiveOwnedDaemonSocket,
    removeOwnedDaemonAuthMaterial,
    verifyPrivateDaemonRuntime,
)
from ._instance_gc import (
    _reap_daemon_tmp_artifacts,
    _reap_dead_daemon_instance_dirs,
)


_SUPERVISORS: dict[str, dict[str, Any]] = {}
_DAEMON_PROCESSES: dict[str, subprocess.Popen[Any]] = {}


def spawned_daemon_process(instance_id: str) -> subprocess.Popen[Any] | None:
    """The Popen this runtime spawned for an instance, if it owns one."""
    return _DAEMON_PROCESSES.get(instance_id)


def _required_instance_id(values: dict[str, Any]) -> str:
    instance_id = values.get("instanceId") or values.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("Daemon operations require an explicit runtime instanceId.")
    return parse_daemon_instance_id(instance_id)


async def connectTaskSupervisorDaemonClient(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    values = {**(args or {}), **kwargs}
    instance_id = _required_instance_id(values)
    state = await load_daemon_state(instance_id)
    if not state:
        return None
    pid = state.get("pid")
    if not isinstance(pid, int) or not _is_process_alive(pid):
        await _cleanup_stale_daemon_artifacts(instance_id)
        return None
    if state.get("phase") != "running":
        return None
    socket_path = values.get("socketPath") or state.get("socketPath")
    if socket_path != daemon_socket_path(instance_id):
        raise DaemonProtocolError(
            "daemon.auth_failed",
            "Daemon state names an unexpected socket path.",
        )
    await verifyPrivateDaemonRuntime(instance_id)
    auth = await readDaemonAuthMaterial(instance_id, state)
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
    values = {**(args or {}), **kwargs}
    instance_id = _required_instance_id(values)
    cwd = values.get("cwd") or os.getcwd()
    owner_pid = values.get("ownerPid") if isinstance(values.get("ownerPid"), int) else os.getpid()
    await prepareDaemonRuntimeSecurity({"instanceId": instance_id, "requireStoppedForMigration": True})
    await ensure_base_layout(cwd)
    await _reap_dead_daemon_instance_dirs()
    budget = _resolve_startup_timeout_seconds(values)
    async with with_record_transaction(
        f"daemon-ensure-{instance_id}",
        get_state_root(),
        timeoutMs=int((budget + 5) * 1000),
    ):
        client = await _ensure_daemon_under_lock(instance_id, cwd, values, budget, owner_pid)
    _SUPERVISORS[instance_id] = client
    return client


async def _ensure_daemon_under_lock(
    instance_id: str,
    cwd: str,
    args: dict[str, Any],
    budget: float,
    owner_pid: int,
) -> dict[str, Any]:
    existing_client = await connectTaskSupervisorDaemonClient({"instanceId": instance_id})
    if existing_client is not None:
        return existing_client
    state = await load_daemon_state(instance_id)
    pid = (state or {}).get("pid")
    if isinstance(pid, int) and _is_process_alive(pid):
        if _daemon_state_is_still_booting(state):
            client = await _wait_for_running_daemon(instance_id, pid, budget)
            if client is not None:
                return client
            if _is_process_alive(pid):
                raise RuntimeError(
                    f"Task supervisor daemon (pid {pid}) is still starting — phase {state.get('phase')}."
                )
        else:
            terminated = await _terminate_wedged_daemon(instance_id, pid)
            if not terminated and _is_process_alive(pid):
                raise RuntimeError(
                    f"Refusing to replace live pid {pid}: it could not be verified as this runtime's daemon."
                )
    await _cleanup_stale_daemon_artifacts(instance_id)
    last_error: BaseException | None = None
    for attempt in (1, 2):
        process = await _spawn_detached_daemon(instance_id, cwd, owner_pid)
        deadline = asyncio.get_running_loop().time() + budget
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            if process.poll() is not None:
                last_error = RuntimeError(
                    f"Task supervisor daemon exited during startup with code {process.returncode}."
                )
                break
            client = await connectTaskSupervisorDaemonClient({"instanceId": instance_id})
            if client is not None:
                return client
        if process.poll() is None:
            last_error = RuntimeError("Timed out waiting for task supervisor daemon to start.")
            break
        if attempt == 1:
            await _cleanup_stale_daemon_artifacts(instance_id)
    # No degraded modes: daemon spawn failure is a loud hard error and the
    # remedy is a daemon restart, never unsupervised in-process execution.
    raise RuntimeError(
        f"Unable to start task supervisor daemon: {last_error}; "
        f"see {os.path.join(daemon_logs_dir(instance_id), 'bootstrap.log')}"
    )


async def stopTaskSupervisorDaemon(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    values = {**(args or {}), **kwargs}
    instance_id = _required_instance_id(values)
    supervisor = _SUPERVISORS.get(instance_id)
    client = (
        supervisor
        if (
            supervisor
            and supervisor.get("_daemonClient")
            and not supervisor.get("_isClosed", lambda: False)()
        )
        else await connectTaskSupervisorDaemonClient({"instanceId": instance_id})
    )
    if client and client.get("_daemonClient"):
        with contextlib.suppress(Exception):
            await client["_request"]("shutdown", {})
        await client["close"]()
    elif supervisor:
        await supervisor["close"]()
    state = await load_daemon_state(instance_id)
    pid = (state or {}).get("pid")
    if isinstance(pid, int) and pid != os.getpid():
        await _wait_for_process_exit(pid, 2)
        if _is_process_alive(pid) and await _is_daemon_run_process(pid, instance_id):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
            await _wait_for_process_exit(pid, 2)
    process = _DAEMON_PROCESSES.get(instance_id)
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
    _SUPERVISORS.pop(instance_id, None)
    _DAEMON_PROCESSES.pop(instance_id, None)
    await _cleanup_stale_daemon_artifacts(instance_id)


async def getTaskSupervisorDaemonState(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    values = {**(args or {}), **kwargs}
    instance_id = _required_instance_id(values)
    state = await load_daemon_state(instance_id)
    client = await connectTaskSupervisorDaemonClient({"instanceId": instance_id})
    if state and client:
        with contextlib.suppress(Exception):
            state = {**state, "supervisor": await client["_request"]("getState", {})}
        if client.get("_daemonClient"):
            await client["close"]()
    return state


async def _spawn_detached_daemon(instance_id: str, cwd: str, owner_pid: int) -> subprocess.Popen[Any]:
    await ensure_base_layout(cwd)
    os.makedirs(daemon_logs_dir(instance_id), mode=0o700, exist_ok=True)
    os.chmod(daemon_logs_dir(instance_id), 0o700)
    log_path = os.path.join(daemon_logs_dir(instance_id), "bootstrap.log")
    env = dict(os.environ)
    env["AGENT_CORE_HOME"] = get_state_root()
    repo_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = repo_root if not env.get("PYTHONPATH") else f"{repo_root}{os.pathsep}{env['PYTHONPATH']}"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_core.process.daemon_worker",
                "--cwd",
                cwd,
                "--instance-id",
                instance_id,
                "--owner-pid",
                str(owner_pid),
            ],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        os.close(fd)
    _DAEMON_PROCESSES[instance_id] = process
    await asyncio.to_thread(_append_spawn_ledger, instance_id, process.pid)
    return process


async def _cleanup_stale_daemon_artifacts(instance_id: str) -> None:
    async with with_record_transaction(
        f"daemon-instance-{instance_id}",
        get_state_root(),
        timeoutMs=_daemon_instance_wait_ms(),
    ):
        state = await load_daemon_state(instance_id)
        if state and isinstance(state.get("pid"), int) and _is_process_alive(state["pid"]):
            return
        current = await load_daemon_state(instance_id)
        if state != current:
            return
        socket_path = daemon_socket_path(instance_id)
        with contextlib.suppress(FileNotFoundError):
            await removeInactiveOwnedDaemonSocket(socket_path)
        token_id = (state or {}).get("authTokenId")
        if isinstance(token_id, str):
            await removeDaemonAuthMaterialIfOwned(instance_id, token_id)
        else:
            await removeOwnedDaemonAuthMaterial(instance_id)
        await clear_daemon_state(instance_id)
        await _reap_daemon_tmp_artifacts(instance_id)


async def _wait_for_running_daemon(instance_id: str, pid: int, timeout: float) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not _is_process_alive(pid):
            return None
        client = await connectTaskSupervisorDaemonClient({"instanceId": instance_id})
        if client is not None:
            return client
        await asyncio.sleep(0.05)
    return None


def _resolve_startup_timeout_seconds(args: dict[str, Any]) -> float:
    from ..config._knobs import knobValue

    if os.environ.get("AGENT_CORE_DAEMON_START_TIMEOUT_MS"):
        # The legacy env name IS the knob's canonical env override; when
        # set it keeps beating per-call arguments, as it always has.
        return max(0.025, int(knobValue("daemon.start_timeout_ms")) / 1000)
    if args.get("startupTimeoutSeconds") is not None:
        return max(0.025, float(args.get("startupTimeoutSeconds") or 0))
    if args.get("startTimeoutMs") is not None:
        return max(0.025, float(args.get("startTimeoutMs") or 0) / 1000)
    return max(0.025, int(knobValue("daemon.start_timeout_ms")) / 1000)


def _daemon_state_is_still_booting(state: dict[str, Any] | None) -> bool:
    if not state or state.get("phase") == "running":
        return False
    from ..config._knobs import knobValue

    boot_grace_seconds = int(knobValue("daemon.boot_grace_ms")) / 1000
    try:
        import datetime

        started = datetime.datetime.fromisoformat(str(state.get("startedAt")).replace("Z", "+00:00"))
        age = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
    except Exception:
        age = boot_grace_seconds + 1
    return age < boot_grace_seconds


async def _terminate_wedged_daemon(instance_id: str, pid: int) -> bool:
    if not await _is_daemon_run_process(pid, instance_id):
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    await _wait_for_process_exit(pid, 2)
    if _is_process_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    return not _is_process_alive(pid)


async def _is_daemon_run_process(pid: int, instance_id: str) -> bool:
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
    return (
        result.returncode == 0
        and "agent_core.process.daemon_worker" in result.stdout
        and instance_id in result.stdout
    )


async def _wait_for_process_exit(pid: int, timeout_seconds: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if not _is_process_alive(pid):
            return
        await asyncio.sleep(0.05)


def _append_spawn_ledger(instance_id: str, pid: int) -> None:
    path = daemon_spawn_ledger_path(instance_id)
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


def _daemon_instance_wait_ms() -> int:
    from ..config._knobs import knobValue

    return int(knobValue("daemon.instance_lock_timeout_ms"))
