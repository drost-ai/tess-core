from __future__ import annotations

import asyncio
import signal
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from ._utils import now_iso
from .process_identity import ProcessIdentityError, captureOwnedProcessIdentity, signalOwnedProcess
from .runtime.identifiers import parseSessionId, parseTaskId


@dataclass(slots=True)
class ReservedProcessLaunch:
    child: subprocess.Popen[Any]
    completion: "asyncio.Task[dict[str, Any]]"
    identity: dict[str, Any] | None = None
    completedBeforeAttach: dict[str, Any] | None = None


def createTaskLaunchReservation(
    args: Mapping[str, Any] | None = None,
    *,
    taskId: str | None = None,
    sessionId: str | None = None,
    kind: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    values = dict(args or {})
    task_id = parseTaskId(values.get("taskId", taskId))
    session_id = parseSessionId(values.get("sessionId", sessionId))
    task_kind = values.get("kind", kind)
    if task_kind not in {"background_command", "main_session", "subagent"}:
        raise ValueError(f"Unsupported task launch reservation kind: {task_kind}")
    return {
        "schemaVersion": 1,
        "reservationId": str(uuid.uuid4()),
        "taskId": task_id,
        "sessionId": session_id,
        "kind": task_kind,
        "state": "starting",
        "createdAt": values.get("now", now) or now_iso(),
    }


def attachTaskLaunchReservation(reservation: Mapping[str, Any], now: str | None = None) -> dict[str, Any]:
    _assert_starting_reservation(reservation)
    return {**reservation, "state": "attached", "attachedAt": now or now_iso()}


def failTaskLaunchReservation(
    reservation: Mapping[str, Any],
    error: Any,
    now: str | None = None,
) -> dict[str, Any]:
    return {
        **reservation,
        "state": "failed",
        "failedAt": now or now_iso(),
        "failureReason": _bounded_error(error),
    }


async def launchReservedProcess(
    *,
    reservation: Mapping[str, Any],
    expectedArgv: list[str],
    reserve: Callable[[], Awaitable[None]],
    spawn: Callable[[], subprocess.Popen[Any]],
    attach: Callable[[dict[str, Any]], Awaitable[None]],
    fail: Callable[[Any], Awaitable[None]],
    cleanupTimeoutMs: int = 3000,
    captureIdentity: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> ReservedProcessLaunch:
    _assert_starting_reservation(reservation)
    await reserve()
    try:
        child = spawn()
    except BaseException as exc:
        await fail(exc)
        raise

    completion = asyncio.create_task(_wait_for_process(child))
    identity: dict[str, Any] | None = None
    try:
        capture = captureIdentity or captureOwnedProcessIdentity
        identity = await capture(
            {
                "pid": child.pid,
                "expectedArgv": expectedArgv,
                "spawnedAt": reservation["createdAt"],
            }
        )
        await attach(identity)
        completed = completion.result() if completion.done() and not completion.cancelled() else None
        return ReservedProcessLaunch(child, completion, identity, completed)
    except BaseException as exc:
        if isinstance(exc, ProcessIdentityError) and exc.code == "process.not_found":
            terminal = completion.result() if completion.done() and not completion.cancelled() else await _wait_for_completion(completion, 100)
            if terminal is not None:
                await fail(exc)
                return ReservedProcessLaunch(child, completion, None, terminal)

        if identity is not None:
            try:
                await signalOwnedProcess(identity, signal.SIGTERM)
            except Exception:
                pass
        elif child.poll() is None:
            # The handle came directly from the just-completed spawn and was
            # never persisted. A bare persisted PID is never used here.
            try:
                child.terminate()
            except ProcessLookupError:
                pass
        await _wait_for_completion(completion, cleanupTimeoutMs)
        await fail(exc)
        raise


def _assert_starting_reservation(reservation: Mapping[str, Any]) -> None:
    try:
        valid = (
            reservation.get("schemaVersion") == 1
            and reservation.get("state") == "starting"
            and bool(reservation.get("reservationId"))
            and parseTaskId(reservation.get("taskId")) == reservation.get("taskId")
            and parseSessionId(reservation.get("sessionId")) == reservation.get("sessionId")
        )
    except Exception:
        valid = False
    if not valid:
        raise ValueError("Task launch reservation is not in a valid starting state.")


async def _wait_for_process(child: subprocess.Popen[Any]) -> dict[str, Any]:
    code = child.poll()
    while code is None:
        await asyncio.sleep(0.02)
        code = child.poll()
    if code is not None and code < 0:
        try:
            signal_name = signal.Signals(-code).name
        except ValueError:
            signal_name = f"SIG{-code}"
        return {"type": "exit", "code": None, "signal": signal_name}
    return {"type": "exit", "code": code, "signal": None}


async def _wait_for_completion(
    completion: "asyncio.Task[dict[str, Any]]",
    timeout_ms: int,
) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(asyncio.shield(completion), max(0, timeout_ms) / 1000)
    except asyncio.TimeoutError:
        return None


def _bounded_error(error: Any) -> str:
    value = str(error)
    return value if len(value) <= 500 else f"{value[:497]}..."


create_task_launch_reservation = createTaskLaunchReservation
attach_task_launch_reservation = attachTaskLaunchReservation
fail_task_launch_reservation = failTaskLaunchReservation
launch_reserved_process = launchReservedProcess
