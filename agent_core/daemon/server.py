from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import uuid
from typing import Any

from .._utils import now_iso
from ..storage import (
    append_daemon_event,
    daemon_pid_path,
    daemon_socket_path,
    daemon_state_path,
    ensure_base_layout,
    get_state_root,
    load_daemon_state,
    read_last_daemon_event_cursor,
    rotate_daemon_events_if_oversized,
    save_daemon_state,
    with_record_transaction,
)
from ..tasks.supervisor import create_task_supervisor
from .events import (
    _publish_daemon_event,
    _record_daemon_lifecycle_event,
    _replay_daemon_events,
    _send_bootstrap_snapshots,
)
from .lifecycle import (
    _cleanup_stale_daemon_artifacts,
    _daemon_instance_wait_ms,
    _is_process_alive,
)
from .protocol import DAEMON_PROTOCOL_VERSION
from .requests import _handle_daemon_request, _normalise_daemon_method
from .security import (
    DaemonSecurityError,
    daemon_auth_token_path,
    generate_daemon_auth_material,
    publish_daemon_auth_material,
    remove_daemon_auth_material_if_owned,
    verify_owned_daemon_socket,
)
from .security_tree import prepare_daemon_runtime_security
from .transport import (
    AuthenticatedDaemonServer,
    DaemonAbortSignal,
    DaemonServerPeer,
    create_authenticated_daemon_server,
)


async def run_task_supervisor_daemon(cwd: str, instanceId: str, ownerPid: int | None = None) -> None:
    await prepare_daemon_runtime_security({"instanceId": instanceId, "requireStoppedForMigration": True})
    wait_ms = _daemon_instance_wait_ms()
    async with with_record_transaction(
        f"daemon-instance-{instanceId}",
        get_state_root(),
        timeoutMs=wait_ms,
    ):
        await _run_task_supervisor_daemon_scoped(cwd, instanceId, ownerPid)


async def _run_task_supervisor_daemon_scoped(cwd: str, instance_id: str, owner_pid: int | None = None) -> None:
    await ensure_base_layout(cwd)
    socket_path = daemon_socket_path(instance_id)
    existing = await load_daemon_state(instance_id)
    existing_pid = (existing or {}).get("pid")
    if isinstance(existing_pid, int) and _is_process_alive(existing_pid):
        raise RuntimeError(f"Daemon already running with pid {existing_pid}")
    await _cleanup_stale_daemon_artifacts(instance_id)
    await rotate_daemon_events_if_oversized(instance_id)
    cursor = max(await read_last_daemon_event_cursor(instance_id), int((existing or {}).get("cursor") or 0))
    daemon_epoch = str(uuid.uuid4())
    daemon_started_at = now_iso()
    daemon_phase = "starting"
    auth = generate_daemon_auth_material()
    subscribers: dict[str, dict[str, Any]] = {}
    shutdown = asyncio.Event()
    supervisor: dict[str, Any] | None = None
    transport: AuthenticatedDaemonServer | None = None
    rotation_task: asyncio.Task[Any] | None = None
    owner_watch_task: asyncio.Task[Any] | None = None
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
            await save_daemon_state(
                instance_id,
                {
                    "instanceId": instance_id,
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
        await append_daemon_event(instance_id, payload)
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
            return await _handle_daemon_request(instance_id, supervisor or {}, shutdown, request, signal)
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
            await _replay_daemon_events(instance_id, peer, session_id, after_cursor)
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
        transport = create_authenticated_daemon_server(
            token=auth["token"],
            epoch=daemon_epoch,
            dispatch=dispatch,
            onAuthenticated=on_authenticated,
            onDisconnect=on_disconnect,
            accepting=False,
        )
        _inject_daemon_boot_fault("pre_bind", instance_id)
        await transport.listen(socket_path)
        os.chmod(socket_path, 0o600)
        verify_owned_daemon_socket(socket_path)
        _inject_daemon_boot_fault("post_bind", instance_id)
        await publish_daemon_auth_material(instance_id, auth)
        await write_daemon_state("starting")
        _inject_daemon_boot_fault("post_publish", instance_id)
        await _record_daemon_lifecycle_event(
            instance_id,
            "daemon.recovering",
            {"phase": "recovering", "epoch": daemon_epoch},
            next_event_cursor,
        )
        await write_daemon_state("recovering")
        supervisor = await create_task_supervisor({"cwd": cwd, "onEvent": on_event})
        rotation_task = asyncio.create_task(_rotate_daemon_events_periodically(instance_id, shutdown))
        if isinstance(owner_pid, int) and owner_pid > 0:
            owner_watch_task = asyncio.create_task(
                _watch_owner_runtime(instance_id, owner_pid, daemon_epoch, shutdown, next_event_cursor, supervisor)
            )
        _inject_daemon_boot_fault("during_recovery", instance_id)
        await write_daemon_state("running")
        await _record_daemon_lifecycle_event(
            instance_id,
            "daemon.recovered",
            {"phase": "running", "epoch": daemon_epoch},
            next_event_cursor,
        )
        transport.accepting = True
        await shutdown.wait()
    finally:
        daemon_phase = "shutting_down"
        for watch_task in (rotation_task, owner_watch_task):
            if watch_task:
                watch_task.cancel()
                await asyncio.gather(watch_task, return_exceptions=True)
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
            instance_id,
            daemon_epoch,
            os.getpid(),
            auth["tokenId"],
            socket_path,
        )
        for signum in installed_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)


async def _clear_owned_daemon_instance(
    instance_id: str,
    epoch: str,
    pid: int,
    token_id: str,
    socket_path: str,
) -> None:
    state = await load_daemon_state(instance_id)
    if state and (state.get("epoch") != epoch or state.get("pid") != pid):
        return
    socket_info = verify_owned_daemon_socket(socket_path)
    for target in (daemon_pid_path(instance_id), daemon_state_path(instance_id)):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(target)
    await remove_daemon_auth_material_if_owned(instance_id, token_id)
    if socket_info:
        current = verify_owned_daemon_socket(socket_path)
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


async def _watch_owner_runtime(
    instance_id: str,
    owner_pid: int,
    epoch: str,
    shutdown: asyncio.Event,
    next_cursor: Any,
    supervisor: dict[str, Any] | None = None,
) -> None:
    """Direction-1 dead-man switch: the daemon dies with its owner runtime.

    The owner's observed identity is captured at boot so a recycled pid can
    never masquerade as a live owner. Owner death triggers the normal
    shutdown path, whose supervisor close escalates every owned worker
    through the shared TERM->KILL ladder."""
    from ..config._knobs import knob_value
    from ..process.identity import _inspect_process_identity

    baseline = None
    with contextlib.suppress(Exception):
        baseline = await _inspect_process_identity(owner_pid)
    while not shutdown.is_set():
        await asyncio.sleep(int(knob_value("daemon.owner_poll_ms")) / 1000)
        owner_alive = _is_process_alive(owner_pid)
        if owner_alive and baseline is not None:
            try:
                current = await _inspect_process_identity(owner_pid)
                owner_alive = current["startToken"] == baseline["startToken"]
            except Exception:
                owner_alive = _is_process_alive(owner_pid)
        if owner_alive:
            continue
        with contextlib.suppress(Exception):
            await _record_daemon_lifecycle_event(
                instance_id,
                "daemon.owner_lost",
                {"ownerPid": owner_pid, "epoch": epoch},
                next_cursor,
            )
        if supervisor is not None:
            # The workers die with the runtime that owned them, and their
            # records say so: runtime_stopped, never resumed (the shutdown's
            # own escalation then finds nothing left to stop).
            from .session_stop import stop_all_workers

            with contextlib.suppress(Exception):
                await stop_all_workers(supervisor, {"reason": "runtime_stopped"})
        shutdown.set()
        return


async def _rotate_daemon_events_periodically(instance_id: str, shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            from ..config._knobs import knob_value

            await asyncio.wait_for(shutdown.wait(), timeout=int(knob_value("daemon.events_rotate_check_ms")) / 1000)
        except asyncio.TimeoutError:
            await rotate_daemon_events_if_oversized(instance_id)


def _inject_daemon_boot_fault(stage: str, instance_id: str) -> None:
    # Two-key arming: a staged boot crash (os._exit) in a production daemon
    # must never be one stray environment variable away. The harness sets
    # both keys; either alone is inert.
    if os.environ.get("AGENT_CORE_TEST_FAULTS") != "daemon-boot":
        return
    if os.environ.get("AGENT_CORE_TEST_DAEMON_BOOT_FAULT_STAGE") != stage:
        return
    marker = os.environ.get("P02_FAULT_MARKER")
    if marker:
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                state = _read_fault_state(instance_id)
                payload = json.dumps(
                    {
                        "stage": stage,
                        "socketExists": os.path.lexists(daemon_socket_path(instance_id)),
                        "tokenExists": os.path.lexists(daemon_auth_token_path(instance_id)),
                        "stateExists": os.path.lexists(daemon_state_path(instance_id)),
                        "statePhase": state.get("phase") if state else None,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                raw = memoryview(payload.encode("utf-8"))
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


def _read_fault_state(instance_id: str) -> dict[str, Any] | None:
    try:
        with open(daemon_state_path(instance_id), "rb") as handle:
            payload = handle.read(4_097)
        if len(payload) > 4_096:
            return None
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
