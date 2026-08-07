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
    appendDaemonEvent,
    clearDaemonState,
    daemonLogsDir,
    daemonSocketPath,
    daemonSpawnLedgerPath,
    daemonStatePath,
    ensureBaseLayout,
    getStateRoot,
    loadDaemonState,
    parseDaemonInstanceId,
    saveDaemonState,
    withRecordTransaction,
)
from ..tasks.supervisor import createTaskSupervisor
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
from .in_process import _install_in_process_subscription_parity
from ._instance_gc import (
    _reap_daemon_tmp_artifacts,
    _reap_dead_daemon_instance_dirs,
)


_SUPERVISORS: dict[str, dict[str, Any]] = {}
_DAEMON_PROCESSES: dict[str, subprocess.Popen[Any]] = {}
_DAEMON_BOOT_GRACE_SECONDS = 5 * 60


def _required_instance_id(values: dict[str, Any]) -> str:
    instance_id = values.get("instanceId") or values.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("Daemon operations require an explicit runtime instanceId.")
    return parseDaemonInstanceId(instance_id)


async def connectTaskSupervisorDaemonClient(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    values = {**(args or {}), **kwargs}
    instance_id = _required_instance_id(values)
    state = await loadDaemonState(instance_id)
    if not state:
        return _local_supervisor_fallback(instance_id)
    pid = state.get("pid")
    if not isinstance(pid, int) or not _is_process_alive(pid):
        await _cleanup_stale_daemon_artifacts(instance_id)
        return None
    if state.get("phase") != "running":
        return None
    socket_path = values.get("socketPath") or state.get("socketPath")
    if socket_path != daemonSocketPath(instance_id):
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
    if values.get("inProcess"):
        return await _ensure_in_process_supervisor(instance_id, cwd)
    await prepareDaemonRuntimeSecurity({"instanceId": instance_id, "requireStoppedForMigration": True})
    await ensureBaseLayout(cwd)
    await _reap_dead_daemon_instance_dirs()
    budget = _resolve_startup_timeout_seconds(values)
    async with withRecordTransaction(
        f"daemon-ensure-{instance_id}",
        getStateRoot(),
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
    state = await loadDaemonState(instance_id)
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
    message = (
        f"Unable to start task supervisor daemon: {last_error}; "
        f"see {os.path.join(daemonLogsDir(instance_id), 'bootstrap.log')}"
    )
    if args.get("allowInProcessFallback", True):
        await _record_daemon_unavailable(instance_id, "spawn_failed", message)
        return await _ensure_in_process_supervisor(instance_id, cwd)
    raise RuntimeError(message)


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
    state = await loadDaemonState(instance_id)
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
    state = await loadDaemonState(instance_id)
    local = _local_supervisor_fallback(instance_id)
    if local is not None:
        local_state = local["getState"]()
        return {**(state or {}), **local_state, "supervisor": local_state}
    client = await connectTaskSupervisorDaemonClient({"instanceId": instance_id})
    if state and client:
        with contextlib.suppress(Exception):
            state = {**state, "supervisor": await client["_request"]("getState", {})}
        if client.get("_daemonClient"):
            await client["close"]()
    return state


async def _ensure_in_process_supervisor(instance_id: str, cwd: str) -> dict[str, Any]:
    existing = _SUPERVISORS.get(instance_id)
    if existing is not None and not existing.get("_daemonClient"):
        return existing

    async def on_event(event: dict[str, Any]) -> None:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        await appendDaemonEvent(
            instance_id,
            {
                "createdAt": now_iso(),
                "taskId": data.get("task_id") if isinstance(data.get("task_id"), str) else None,
                **event,
            },
        )

    supervisor = await createTaskSupervisor({"cwd": cwd, "onEvent": on_event})
    _SUPERVISORS[instance_id] = supervisor
    _install_in_process_subscription_parity(instance_id, supervisor)
    await saveDaemonState(
        instance_id,
        {
            "instanceId": instance_id,
            "pid": os.getpid(),
            "cwd": cwd,
            "startedAt": now_iso(),
            "mode": "in_process_fallback",
            "status": "degraded",
            "daemonUnavailable": {
                "mode": "in_process_fallback",
                "reason": "detached_daemon_unavailable",
            },
        },
    )
    await appendDaemonEvent(
        instance_id,
        {
            "createdAt": now_iso(),
            "event": "daemon.unavailable",
            "data": {
                "mode": "in_process_fallback",
                "reason": "detached_daemon_unavailable",
            },
        },
    )
    return supervisor


async def _spawn_detached_daemon(instance_id: str, cwd: str, owner_pid: int) -> subprocess.Popen[Any]:
    await ensureBaseLayout(cwd)
    os.makedirs(daemonLogsDir(instance_id), mode=0o700, exist_ok=True)
    os.chmod(daemonLogsDir(instance_id), 0o700)
    log_path = os.path.join(daemonLogsDir(instance_id), "bootstrap.log")
    env = dict(os.environ)
    env["AGENT_CORE_HOME"] = getStateRoot()
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
    async with withRecordTransaction(
        f"daemon-instance-{instance_id}",
        getStateRoot(),
        timeoutMs=_daemon_instance_wait_ms(),
    ):
        state = await loadDaemonState(instance_id)
        if state and isinstance(state.get("pid"), int) and _is_process_alive(state["pid"]):
            return
        current = await loadDaemonState(instance_id)
        if state != current:
            return
        socket_path = daemonSocketPath(instance_id)
        with contextlib.suppress(FileNotFoundError):
            await removeInactiveOwnedDaemonSocket(socket_path)
        token_id = (state or {}).get("authTokenId")
        if isinstance(token_id, str):
            await removeDaemonAuthMaterialIfOwned(instance_id, token_id)
        else:
            await removeOwnedDaemonAuthMaterial(instance_id)
        await clearDaemonState(instance_id)
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


async def _record_daemon_unavailable(instance_id: str, reason: str, detail: str) -> None:
    await appendDaemonEvent(
        instance_id,
        {
            "createdAt": now_iso(),
            "event": "daemon.unavailable",
            "data": {"mode": "in_process_fallback", "reason": reason, "detail": detail},
        },
    )


async def _wait_for_process_exit(pid: int, timeout_seconds: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if not _is_process_alive(pid):
            return
        await asyncio.sleep(0.05)


def _append_spawn_ledger(instance_id: str, pid: int) -> None:
    path = daemonSpawnLedgerPath(instance_id)
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


def _local_supervisor_fallback(instance_id: str) -> dict[str, Any] | None:
    supervisor = _SUPERVISORS.get(instance_id)
    return supervisor if supervisor and not supervisor.get("_daemonClient") else None


def _daemon_instance_wait_ms() -> int:
    try:
        value = int(os.environ.get("AGENT_CORE_DAEMON_INSTANCE_LOCK_TIMEOUT_MS") or 5_000)
    except ValueError:
        value = 5_000
    return min(30_000, max(100, value))
