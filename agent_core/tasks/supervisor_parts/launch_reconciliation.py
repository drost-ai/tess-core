from __future__ import annotations

from .shared import *
from .mappers import _task_record_event_payload
from ...process.task_launch import failTaskLaunchReservation


_ACTIVE_CHILD_STATUSES = {"starting", "backgrounded", "running", "awaiting_input"}


def _has_interrupted_launch(record: dict[str, Any], meta: dict[str, Any]) -> bool:
    reservations = (record.get("launchReservation"), meta.get("launchReservation"))
    return any(
        isinstance(reservation, dict) and reservation.get("state") == "starting"
        for reservation in reservations
    )


async def _reconcile_interrupted_launch(
    task_id: str,
    record: dict[str, Any],
    meta: dict[str, Any],
    supervisor_id: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> None:
    reservation = next(
        reservation
        for reservation in (record.get("launchReservation"), meta.get("launchReservation"))
        if isinstance(reservation, dict) and reservation.get("state") == "starting"
    )
    identity_error: str | None = None
    try:
        identity = processIdentityFromRecord(record, meta)
    except ProcessIdentityError as exc:
        identity = None
        identity_error = exc.code
    if identity is not None:
        identity_error = await _drain_interrupted_launch(identity)

    reason = "Task launch was interrupted before durable attachment; retry is safe."
    failed = failTaskLaunchReservation(reservation, reason)
    now = failed["failedAt"]
    recovery_metadata = {"launch_recovery_state": "interrupted_before_attachment"}
    if identity_error is not None:
        recovery_metadata["process_identity_error"] = identity_error
    next_meta = {
        **meta,
        "type": meta.get("type") or record.get("kind"),
        "taskId": task_id,
        "status": "failed",
        "detailStatus": "launch_failed",
        "error": reason,
        "updatedAt": now,
        "finishedAt": now,
        "retryable": True,
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "launchReservation": failed,
        "metadata": {**dict(meta.get("metadata") or {}), **recovery_metadata},
    }
    next_record = {
        **record,
        "status": "failed",
        "summary": reason,
        "updatedAt": now,
        "completedAt": now,
        "detailStatus": "launch_failed",
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "launchReservation": failed,
        "metadata": {**dict(record.get("metadata") or {}), **recovery_metadata},
    }
    await writeTaskMeta(task_id, next_meta)
    await saveTaskRecord(next_record)
    session_id = await _repair_interrupted_launch_links(task_id, record, meta)
    if record.get("kind") == "subagent":
        agent_id = meta.get("agent_id") or record.get("sourceId")
        if isinstance(agent_id, str) and agent_id:
            await mutateChildAgent(
                agent_id,
                lambda child: (
                    {
                        **child,
                        "status": "failed",
                        "error": child.get("error") or reason,
                        "completionReason": "worker_failed",
                        "completedAt": child.get("completedAt") or now,
                        "lastUpdatedAt": now,
                        "lastSummary": reason,
                        "retryable": True,
                        "stoppable": False,
                    }
                    if child and child.get("status") in _ACTIVE_CHILD_STATUSES
                    else child
                ),
            )
    await maybe_await(
        emit("task.failed", _task_record_event_payload(next_record), session_id)
    )


async def _drain_interrupted_launch(identity: dict[str, Any]) -> str | None:
    try:
        await signalOwnedProcess(identity, signal.SIGTERM)
    except ProcessIdentityError as exc:
        return exc.code
    except OSError:
        return "process.not_found"
    for _ in range(150):
        await asyncio.sleep(0.02)
        try:
            await verifyOwnedProcessIdentity(identity)
        except ProcessIdentityError as exc:
            return exc.code
    try:
        await signalOwnedProcess(identity, signal.SIGKILL)
    except ProcessIdentityError as exc:
        return exc.code
    except OSError:
        return "process.not_found"
    for _ in range(50):
        await asyncio.sleep(0.02)
        try:
            await verifyOwnedProcessIdentity(identity)
        except ProcessIdentityError as exc:
            return exc.code
    return "process.drain_timeout"


async def _repair_interrupted_launch_links(
    task_id: str,
    record: dict[str, Any],
    meta: dict[str, Any],
) -> str | None:
    kind = record.get("kind") or meta.get("type")
    parent_session_id = (
        meta.get("parent_session_id")
        if kind == "subagent"
        else record.get("sessionId") or meta.get("sessionId")
    )
    if isinstance(parent_session_id, str) and parent_session_id:
        await mutateSessionRecord(
            parent_session_id,
            lambda session: _append_reconciled_task_id(
                session,
                task_id,
                include_background_session=kind == "main_session",
            ),
        )
    child_session_id = meta.get("sessionId") if kind == "subagent" else None
    if (
        isinstance(child_session_id, str)
        and child_session_id
        and child_session_id != parent_session_id
    ):
        await mutateSessionRecord(
            child_session_id,
            lambda session: _bind_reconciled_subagent_task(session, task_id, meta),
        )
    return parent_session_id if isinstance(parent_session_id, str) else None


def _append_reconciled_task_id(
    session: dict[str, Any],
    task_id: str,
    *,
    include_background_session: bool = False,
) -> dict[str, Any]:
    metadata = session.setdefault("metadata", {})
    metadata["taskIds"] = list(
        dict.fromkeys([*(metadata.get("taskIds") or []), task_id])
    )
    if include_background_session:
        metadata["backgroundSessionIds"] = list(
            dict.fromkeys(
                [*(metadata.get("backgroundSessionIds") or []), session["id"]]
            )
        )
    return session


def _bind_reconciled_subagent_task(
    session: dict[str, Any],
    task_id: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    metadata = session.setdefault("metadata", {})
    metadata["subagentTaskId"] = task_id
    runtime_contract = meta.get("runtimeContract")
    if isinstance(runtime_contract, dict) and runtime_contract:
        metadata["subagentRuntimeContract"] = runtime_contract
    return session
