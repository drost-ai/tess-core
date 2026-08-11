from __future__ import annotations

from ...process.identity import (
    ProcessIdentityError,
    process_identity_from_record,
)
from typing import Any, Callable
from ...process.task_launch import fail_task_launch_reservation
from ...process._termination import drain_owned_process
from .finalize_child import finalize_lost_worker


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
        identity = process_identity_from_record(record, meta)
    except ProcessIdentityError as exc:
        identity = None
        identity_error = exc.code
    if identity is not None:
        identity_error = await drain_owned_process(identity)

    reason = "Task launch was interrupted before durable attachment; retry is safe."
    failed = fail_task_launch_reservation(reservation, reason)
    await finalize_lost_worker(
        taskId=task_id,
        cause="launch_interrupted",
        source="launch_reconciliation",
        supervisorId=supervisor_id,
        emit=emit,
        processState={"identity_error": identity_error} if identity_error is not None else None,
        failedReservation=failed,
        summary=reason,
    )
