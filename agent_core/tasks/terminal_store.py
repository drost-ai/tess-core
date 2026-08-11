from __future__ import annotations

import uuid
from typing import Any

from .._utils import now_iso
from ..process.identity import ProcessIdentityError, process_identity_from_record
from ..storage import (
    load_health_check_task,
    load_task_record,
    read_task_meta,
    save_health_check_task,
    save_task_record,
    with_record_transaction,
    write_task_meta,
)
from .terminal_transitions import (
    apply_deliberate_task_stop,
    apply_task_process_completion,
    normalize_task_process_outcome,
    resolve_task_semantic_status,
    to_task_stop_result,
)


async def prepare_task_stop(
    taskId: str,
    signalName: str,
    eventOwner: str = "supervisor",
) -> dict[str, Any] | None:
    async with with_record_transaction("task-lifecycle", taskId):
        record = await load_task_record(taskId)
        meta = await read_task_meta(taskId)
        health = await load_health_check_task(taskId)
        if not record and not meta and not health:
            return None
        current = resolve_task_semantic_status(
            (record or {}).get("status"),
            (health or {}).get("status"),
            (meta or {}).get("status"),
        )
        transition = apply_deliberate_task_stop(current)
        common = {
            "record": record,
            "healthCheck": bool(health),
            "sessionId": (record or {}).get("sessionId")
            or (health or {}).get("sessionId")
            or (meta or {}).get("sessionId"),
            "kind": (record or {}).get("kind") or (meta or {}).get("type"),
            "title": (record or {}).get("title")
            or (health or {}).get("title")
            or (meta or {}).get("title"),
        }
        if not transition["changed"]:
            return {**common, "result": to_task_stop_result(taskId, transition)}
        transition_id = str(uuid.uuid4())
        if health:
            committed = await _persist_terminal_transition(
                taskId=taskId,
                transition=transition,
                transitionId=transition_id,
                record=record,
                meta=meta,
                healthCheck=health,
                eventOwner="supervisor",
            )
            return {
                **common,
                "record": committed.get("record"),
                "result": to_task_stop_result(taskId, transition, transition_id),
                "transitionId": transition_id,
            }
        identity_error: str | None = None
        try:
            identity = process_identity_from_record(record, meta)
        except ProcessIdentityError as exc:
            # No owned process is durably attached (launch window, or a
            # legacy record). Raising here left the task unstoppable until
            # reboot; the caller decides how to finish a processless stop.
            identity = None
            identity_error = exc.code
        intent = {
            "schemaVersion": 1,
            "transitionId": transition_id,
            "requestedAt": now_iso(),
            "signal": signalName,
            "eventOwner": eventOwner,
        }
        if record:
            await save_task_record({**record, "terminationIntent": intent})
        await write_task_meta(taskId, {**(meta or {}), "terminationIntent": intent})
        return {
            **common,
            "transitionId": transition_id,
            "identity": identity,
            "identityError": identity_error,
        }


async def commit_prepared_task_stop(
    taskId: str,
    transitionId: str,
    eventOwner: str = "supervisor",
) -> dict[str, Any]:
    async with with_record_transaction("task-lifecycle", taskId):
        record = await load_task_record(taskId)
        meta = await read_task_meta(taskId)
        current = resolve_task_semantic_status(
            (record or {}).get("status"),
            (meta or {}).get("status"),
        )
        intent = _read_termination_intent(
            (record or {}).get("terminationIntent")
            or (meta or {}).get("terminationIntent")
        )
        runner_committed = (
            current == "stopped"
            and (intent or {}).get("transitionId") == transitionId
            and _has_terminal_transition_id(record, meta, transitionId)
        )
        transition = (
            {
                "previousStatus": "running",
                "status": "stopped",
                "changed": True,
                "event": "task.stopped",
                "summary": "Task stopped by deliberate request.",
            }
            if runner_committed
            else apply_deliberate_task_stop(current)
        )
        if transition["changed"] and (intent or {}).get("transitionId") != transitionId:
            raise RuntimeError(f"Task stop intent changed before commit: {taskId}")
        committed = {"record": record}
        if transition["changed"]:
            committed = await _persist_terminal_transition(
                taskId=taskId,
                transition=transition,
                transitionId=transitionId,
                record=record,
                meta=meta,
                eventOwner=eventOwner,
            )
        return {
            "transition": transition,
            "record": committed.get("record"),
            "result": to_task_stop_result(taskId, transition, transitionId),
        }


async def clear_prepared_task_stop(taskId: str, transitionId: str) -> None:
    async with with_record_transaction("task-lifecycle", taskId):
        record = await load_task_record(taskId)
        meta = await read_task_meta(taskId)
        record_intent = _read_termination_intent((record or {}).get("terminationIntent"))
        if (record_intent or {}).get("transitionId") == transitionId:
            next_record = dict(record or {})
            next_record.pop("terminationIntent", None)
            await save_task_record(next_record)
        intent = _read_termination_intent((meta or {}).get("terminationIntent"))
        if meta and (intent or {}).get("transitionId") == transitionId:
            next_meta = dict(meta)
            next_meta.pop("terminationIntent", None)
            await write_task_meta(taskId, next_meta)


async def commit_task_process_completion(
    *,
    taskId: str,
    code: int | None,
    signalName: str | None,
    error: str | None = None,
) -> dict[str, Any]:
    code, signalName = normalize_task_process_outcome(code, signalName)
    async with with_record_transaction("task-lifecycle", taskId):
        record = await load_task_record(taskId)
        meta = await read_task_meta(taskId)
        current = resolve_task_semantic_status(
            (record or {}).get("status"),
            (meta or {}).get("status"),
        )
        intent = _read_termination_intent(
            (record or {}).get("terminationIntent")
            or (meta or {}).get("terminationIntent")
        )
        if error and current == "running":
            transition = {
                "previousStatus": "running",
                "status": "failed",
                "changed": True,
                "event": "task.failed",
                "summary": error,
            }
        elif error:
            transition = apply_deliberate_task_stop(current)
        else:
            transition = apply_task_process_completion(
                current=current,
                code=code,
                signalName=signalName,
                terminationIntent=intent,
            )
        transition_id = (intent or {}).get("transitionId") or str(uuid.uuid4())
        if not transition["changed"]:
            if meta:
                diagnostics = {**meta, "exitCode": code}
                if signalName:
                    diagnostics["signal"] = signalName
                if error:
                    diagnostics["error"] = error
                diagnostics["finishedAt"] = meta.get("finishedAt") or now_iso()
                await write_task_meta(taskId, diagnostics)
            return {"transition": transition, "record": record}
        committed = await _persist_terminal_transition(
            taskId=taskId,
            transition=transition,
            transitionId=transition_id,
            record=record,
            meta=meta,
            eventOwner="supervisor",
            exitCode=code,
            signalName=signalName,
            error=error,
        )
        return {"transition": transition, "record": committed.get("record")}


async def save_projected_task_record(record: dict[str, Any]) -> dict[str, Any]:
    async with with_record_transaction("task-lifecycle", record["id"]):
        existing = await load_task_record(record["id"])
        if existing and resolve_task_semantic_status(existing.get("status")) != "running":
            return existing
        await save_task_record(record)
        return record


async def _persist_terminal_transition(
    *,
    taskId: str,
    transition: dict[str, Any],
    transitionId: str,
    record: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    eventOwner: str,
    healthCheck: dict[str, Any] | None = None,
    exitCode: int | None = None,
    signalName: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    now = now_iso()
    health = None
    if healthCheck:
        health = {
            **healthCheck,
            "status": "stopped" if transition["status"] == "stopped" else "failed",
            "updatedAt": now,
            "revision": int(healthCheck.get("revision") or 0) + 1,
            "generation": int(healthCheck.get("generation") or 0) + 1,
        }
        health.pop("activeProcessIdentity", None)
    next_record = None
    if record:
        next_record = dict(record)
        next_record.pop("terminationIntent", None)
        metadata = dict(next_record.get("metadata") or {})
        metadata.update(
            {
                "terminal_transition_id": transitionId,
                "terminal_event_owner": eventOwner,
            }
        )
        if signalName:
            metadata["termination_signal"] = signalName
        next_record.update(
            {
                "status": transition["status"],
                "summary": transition["summary"],
                "detailStatus": _detail_status(transition),
                "updatedAt": now,
                "completedAt": now,
                "metadata": metadata,
            }
        )
        await save_task_record(next_record)
    if health:
        await save_health_check_task(health)
    next_meta = dict(meta or {})
    next_meta.pop("terminationIntent", None)
    next_meta.update(
        {
            "status": "terminated" if transition["status"] == "stopped" else transition["status"],
            "summary": transition["summary"],
            "detailStatus": _detail_status(transition),
            "updatedAt": now,
            "finishedAt": now,
            "terminalTransitionId": transitionId,
            "terminalEventOwner": eventOwner,
        }
    )
    if transition["status"] == "stopped":
        next_meta["stoppedAt"] = now
        next_meta.pop("error", None)
    elif transition["status"] == "failed":
        next_meta["error"] = error or transition["summary"]
    if exitCode is not None:
        next_meta["exitCode"] = exitCode
    if signalName:
        next_meta["signal"] = signalName
    if health:
        next_meta["generation"] = health["generation"]
        next_meta["revision"] = health["revision"]
        next_meta.pop("activeProcessIdentity", None)
    await write_task_meta(taskId, next_meta)
    return {"record": next_record}


def _read_termination_intent(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if (
        value.get("schemaVersion") != 1
        or not isinstance(value.get("transitionId"), str)
        or not isinstance(value.get("requestedAt"), str)
        or not isinstance(value.get("signal"), str)
        or value.get("eventOwner") not in {None, "supervisor", "tool"}
    ):
        return None
    return value


def _has_terminal_transition_id(
    record: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    transitionId: str,
) -> bool:
    values = [
        ((record or {}).get("metadata") or {}).get("terminal_transition_id"),
        (meta or {}).get("terminalTransitionId"),
    ]
    ids = [value for value in values if isinstance(value, str)]
    return bool(ids) and all(value == transitionId for value in ids)


def _detail_status(transition: dict[str, Any]) -> str:
    if transition["status"] == "stopped":
        return "stopped_by_request"
    if transition["status"] == "completed":
        return "result_ready"
    return "unexpected_signal" if "unexpected SIG" in transition["summary"] else "failed"


