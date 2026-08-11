"""One terminal finalizer for lost workers.

Three sweeps used to hand-roll this transition (boot reconciliation,
live-work projection, interrupted-launch recovery), and the drift between
them was exactly the artifacts each copy forgot: live-work never wrote the
task record, so a dead subagent held a capacity slot until reboot; no lost
path wrote the parent-facing reconciliation ledger entry, so the parent
transcript never learned a child died on daemon loss; and the launch path
guarded child records with a status set containing an invalid member. Every
lost-worker terminalization now flows through :func:`finalize_lost_worker`:
task meta + task record + child-agent record + parent reconciliation ledger
+ session links, under one task-lifecycle transaction, honoring
first-terminal-wins inside it.
"""
from __future__ import annotations

from typing import Any, Callable

from ..._utils import is_process_alive, maybe_await, now_iso
from ...process.identity import (
    ProcessIdentityError,
    processIdentityFromRecord,
    verifyOwnedProcessIdentity,
)
from ...storage import (
    load_task_record,
    mutate_child_agent,
    mutate_session_record,
    read_task_meta,
    save_task_record,
    with_record_transaction,
    write_task_meta,
)
from ...subagents.reconciliation import create_subagent_reconciliation_for_terminal_child
from ..terminal_transitions import resolve_task_semantic_status
from .mappers import _is_active_child_agent_status, _task_record_event_payload
from .records import _build_task_record
from .shared import _DEAD_WORKER_SUMMARY

LOST_WORKER_CAUSES = {"worker_lost", "daemon_lost", "launch_interrupted"}

_TERMINAL_SEMANTIC_STATUSES = {"completed", "failed", "stopped", "terminated", "cancelled", "truncated"}


def _process_state_metadata(process_state: dict[str, Any] | None) -> dict[str, Any]:
    if not process_state:
        return {}
    result = {
        key: value
        for key, value in {
            "process_state": process_state.get("state"),
            "process_state_source": process_state.get("source"),
        }.items()
        if value is not None
    }
    if isinstance(process_state.get("pid"), int):
        result["process_pid"] = process_state["pid"]
    if process_state.get("status") is not None:
        result["process_meta_status"] = process_state.get("status")
    if process_state.get("identity_error") is not None:
        result["process_identity_error"] = process_state.get("identity_error")
    if process_state.get("kill_outcome") is not None:
        result["process_kill_outcome"] = process_state.get("kill_outcome")
    return result


async def worker_verified_dead(record_like: dict[str, Any]) -> bool:
    """Raw pid liveness is not proof OUR worker is alive: a recycled pid pins
    a dead worker's records live forever. A dead pid proves death; a live pid
    must additionally match the persisted identity, and liveness-indeterminate
    inspection errors count as alive so nothing terminalizes on a shrug."""
    pid = record_like.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not is_process_alive(pid):
        return True
    try:
        identity = processIdentityFromRecord(record_like, record_like)
    except ProcessIdentityError:
        return False
    try:
        await verifyOwnedProcessIdentity(identity)
        return False
    except ProcessIdentityError as exc:
        return exc.code in {
            "process.not_found",
            "process.identity_mismatch",
            "process.group_not_owned",
        }


def _lost_summary(cause: str, is_subagent: bool) -> str:
    if cause == "launch_interrupted":
        return "Task launch was interrupted before durable attachment; retry is safe."
    if cause == "daemon_lost":
        return "The owning daemon was lost while this worker was active; the orphaned worker was terminated at daemon boot."
    if is_subagent:
        return "Daemon restarted while this worker was active and the worker process is no longer running."
    return _DEAD_WORKER_SUMMARY


async def finalize_lost_worker(
    *,
    taskId: str,
    cause: str,
    source: str,
    supervisorId: str | None = None,
    emit: Callable[[str, dict[str, Any], str | None], Any] | None = None,
    processState: dict[str, Any] | None = None,
    failedReservation: dict[str, Any] | None = None,
    summary: str | None = None,
) -> dict[str, Any] | None:
    if cause not in LOST_WORKER_CAUSES:
        raise ValueError(f"Unknown lost-worker cause: {cause}")
    async with with_record_transaction("task-lifecycle", taskId):
        record = await load_task_record(taskId) or {}
        meta = await read_task_meta(taskId) or {}
        if not record and not meta:
            return None
        kind = record.get("kind") or meta.get("type")
        is_subagent = kind == "subagent"
        text = summary or _lost_summary(cause, is_subagent)
        now = now_iso()
        current = resolve_task_semantic_status(record.get("status"), meta.get("status"))
        already_terminal = current in _TERMINAL_SEMANTIC_STATUSES
        extra_metadata: dict[str, Any] = dict(_process_state_metadata(processState))
        if cause == "launch_interrupted":
            extra_metadata["launch_recovery_state"] = "interrupted_before_attachment"
        next_meta = meta
        next_record = record or None
        if not already_terminal:
            next_meta = {
                **meta,
                "type": kind,
                "taskId": taskId,
                "status": "failed" if is_subagent or cause == "launch_interrupted" else "terminated",
                "updatedAt": now,
                "finishedAt": now,
                "owner": meta.get("owner") or record.get("owner") or "daemon",
                "metadata": {**dict(meta.get("metadata") or {}), **extra_metadata},
            }
            if supervisorId:
                next_meta["supervisorId"] = supervisorId
            if processState:
                next_meta["processState"] = processState.get("state") or "failed"
                next_meta["processReconciledAt"] = now
            if is_subagent or cause == "launch_interrupted":
                next_meta["error"] = meta.get("error") or text
                next_meta["summary"] = meta.get("summary") or text
                next_meta["completionReason"] = meta.get("completionReason") or cause
                next_meta["completedAt"] = meta.get("completedAt") or now
            else:
                next_meta.pop("error", None)
                next_meta["summary"] = meta.get("summary") or text
            if cause == "launch_interrupted":
                next_meta["detailStatus"] = "launch_failed"
                next_meta["retryable"] = True
                if failedReservation is not None:
                    next_meta["launchReservation"] = failedReservation
            next_record = {
                **_build_task_record(next_meta, text),
                **record,
                "status": "failed" if is_subagent or cause == "launch_interrupted" else "stopped",
                "summary": text,
                "updatedAt": now,
                "completedAt": now,
                "owner": record.get("owner") or meta.get("owner") or "daemon",
                "metadata": {**dict(record.get("metadata") or {}), **extra_metadata},
            }
            if supervisorId:
                next_record["supervisorId"] = supervisorId
            if cause == "daemon_lost":
                next_record["detailStatus"] = "daemon_lost"
            if cause == "launch_interrupted":
                next_record["detailStatus"] = "launch_failed"
                if failedReservation is not None:
                    next_record["launchReservation"] = failedReservation
            await write_task_meta(taskId, next_meta)
            await save_task_record(next_record)
        child_after: dict[str, Any] | None = None
        agent_id = (
            meta.get("agent_id")
            or (meta.get("metadata") or {}).get("agent_id")
            or ((record.get("metadata") or {}).get("agent_id") if record else None)
            or (record.get("sourceId") if is_subagent else None)
        )
        if is_subagent and isinstance(agent_id, str) and agent_id:
            def _finalize_child_record(current_child: dict[str, Any] | None) -> dict[str, Any] | None:
                if not current_child or not _is_active_child_agent_status(current_child.get("status")):
                    return current_child
                updated = {
                    **current_child,
                    "status": "failed",
                    "error": current_child.get("error") or text,
                    "completionReason": current_child.get("completionReason") or (
                        "worker_failed" if cause == "launch_interrupted" else cause
                    ),
                    "completedAt": current_child.get("completedAt") or now,
                    "lastUpdatedAt": now,
                    "lastSummary": text,
                    "stoppable": False,
                }
                if cause == "launch_interrupted":
                    updated["retryable"] = True
                return updated

            child_after = await mutate_child_agent(agent_id, _finalize_child_record)
        session_id: str | None = None
        if cause == "launch_interrupted":
            session_id = await _repair_interrupted_launch_links(taskId, record, meta)
        else:
            raw_session = record.get("sessionId") or meta.get("sessionId")
            session_id = raw_session if isinstance(raw_session, str) else None
        if is_subagent and not already_terminal:
            # The parent-facing ledger entry: without it the parent transcript
            # never learns the child died on the one path where live events
            # are least likely to have survived.
            await create_subagent_reconciliation_for_terminal_child(
                childRecord=child_after,
                taskMeta=next_meta,
                error=text,
                source=f"lost_worker:{source}",
            )
    if emit is not None and not already_terminal and next_record:
        event = "task.failed" if is_subagent or cause == "launch_interrupted" else "task.stopped"
        await maybe_await(emit(event, _task_record_event_payload(next_record), session_id))
    return {"meta": next_meta, "record": next_record, "changed": not already_terminal}


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
        await mutate_session_record(
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
        await mutate_session_record(
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
    metadata["taskIds"] = list(dict.fromkeys([*(metadata.get("taskIds") or []), task_id]))
    if include_background_session:
        metadata["backgroundSessionIds"] = list(
            dict.fromkeys([*(metadata.get("backgroundSessionIds") or []), session["id"]])
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
