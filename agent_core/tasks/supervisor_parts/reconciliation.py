from __future__ import annotations

from .shared import *


async def _reconcile_persisted_tasks(
    supervisor_id: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> None:
    records = await listTaskRecords()
    active_statuses = {"backgrounded", "running", "reconciling", "degraded"}
    recoverable_kinds = {"background_command", "main_session", "subagent"}
    for record in records:
        try:
            if record.get("owner") != "daemon":
                continue
            if record.get("kind") not in recoverable_kinds or record.get("status") not in active_statuses:
                continue
            task_id = record.get("id") or record.get("taskId")
            if not isinstance(task_id, str) or not task_id:
                continue
            meta = await readTaskMeta(task_id)
            if not meta:
                continue
            process_state = _classify_persisted_process(task_id, record, meta)
            if process_state["state"] in {"completed", "failed"}:
                await _mark_terminal_process_from_meta(task_id, record, meta, supervisor_id, emit, process_state)
            elif process_state["state"] in {"running", "orphaned"} and isinstance(process_state.get("pid"), int):
                await _mark_recovered_process(task_id, int(process_state["pid"]), record, meta, supervisor_id, emit, process_state)
            else:
                await _mark_lost_process(task_id, record, meta, supervisor_id, emit, process_state)
        except Exception as exc:
            task_id = record.get("id") or record.get("taskId")
            await maybe_await(
                emit(
                    "task.reconcile_skipped",
                    {"task_id": task_id, "reason": str(exc), "status": record.get("status"), "kind": record.get("kind")},
                    record.get("sessionId"),
                )
            )


async def _mark_recovered_process(
    task_id: str,
    pid: int,
    record: dict[str, Any],
    meta: dict[str, Any],
    supervisor_id: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
    process_state: dict[str, Any] | None = None,
) -> None:
    now = now_iso()
    metadata = dict(record.get("metadata") or {})
    metadata["recovery_state"] = "recovered_after_daemon_restart"
    metadata.update(_process_state_metadata(process_state))
    meta_metadata = dict(meta.get("metadata") or {})
    meta_metadata["recovery_state"] = "recovered_after_daemon_restart"
    meta_metadata.update(_process_state_metadata(process_state))
    next_meta = {
        **meta,
        "status": "reconciling",
        "updatedAt": now,
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "metadata": meta_metadata,
        "processState": (process_state or {}).get("state"),
        "processReconciledAt": now,
    }
    summary = (
        "Recovered after daemon restart. The worker is still alive, but supervisor tracking is degraded until it exits."
        if record.get("kind") == "subagent"
        else "Recovered after daemon restart. The process is still alive, but lifecycle tracking is degraded until it exits."
    )
    next_record = {
        **_build_task_record(next_meta, summary),
        **record,
        "status": "degraded",
        "summary": summary,
        "updatedAt": now,
        "detailStatus": "recovered_after_daemon_restart",
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "metadata": metadata,
    }
    await writeTaskMeta(task_id, next_meta)
    await saveTaskRecord(next_record)
    if record.get("kind") == "subagent":
        agent_id = meta.get("agent_id") or metadata.get("agent_id") or record.get("sourceId")
        if isinstance(agent_id, str) and agent_id:
            await mutateChildAgent(
                agent_id,
                lambda current: {**current, "recoveryState": "recovered_after_daemon_restart", "lastUpdatedAt": now} if current else None,
            )
    await maybe_await(emit("task.updated", _task_record_event_payload(next_record), record.get("sessionId")))
    watcher = asyncio.create_task(_watch_recovered_process(task_id, pid, record.get("sessionId"), emit))
    _WATCHERS.add(watcher)
    watcher.add_done_callback(_WATCHERS.discard)


async def _mark_lost_process(
    task_id: str,
    record: dict[str, Any],
    meta: dict[str, Any],
    supervisor_id: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
    process_state: dict[str, Any] | None = None,
) -> None:
    if record.get("kind") == "subagent" and await _recover_completed_subagent_task(task_id, record, emit, record.get("sessionId")):
        return
    now = now_iso()
    if record.get("kind") == "subagent":
        summary = "Daemon restarted while this worker was active and the worker process is no longer running."
    else:
        summary = "Daemon restarted while this task was active and the process is no longer running."
    state = (process_state or {}).get("state") or "failed"
    meta_metadata = dict(meta.get("metadata") or {})
    meta_metadata.update(_process_state_metadata(process_state))
    record_metadata = dict(record.get("metadata") or {})
    record_metadata.update(_process_state_metadata(process_state))
    next_meta = {
        **meta,
        "type": record.get("kind"),
        "taskId": task_id,
        "status": "failed",
        "error": summary,
        "updatedAt": now,
        "finishedAt": now,
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "metadata": meta_metadata,
        "processState": state,
        "processReconciledAt": now,
    }
    next_record = {
        **_build_task_record(next_meta, summary),
        **record,
        "status": "failed",
        "summary": summary,
        "updatedAt": now,
        "completedAt": now,
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "metadata": record_metadata,
    }
    await writeTaskMeta(task_id, next_meta)
    await saveTaskRecord(next_record)
    await maybe_await(emit("task.failed", _task_record_event_payload(next_record), record.get("sessionId")))


async def _mark_terminal_process_from_meta(
    task_id: str,
    record: dict[str, Any],
    meta: dict[str, Any],
    supervisor_id: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
    process_state: dict[str, Any],
) -> None:
    if record.get("kind") == "subagent" and await _recover_completed_subagent_task(task_id, record, emit, record.get("sessionId")):
        return
    now = now_iso()
    status = _map_status(meta.get("status"))
    if status not in {"completed", "failed", "stopped", "awaiting_input"}:
        status = "failed"
    meta_metadata = dict(meta.get("metadata") or {})
    meta_metadata.update(_process_state_metadata(process_state))
    next_meta = {
        **meta,
        "type": meta.get("type") or record.get("kind"),
        "taskId": task_id,
        "updatedAt": meta.get("updatedAt") or meta.get("finishedAt") or now,
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "metadata": meta_metadata,
        "processState": process_state.get("state"),
        "processReconciledAt": now,
    }
    summary = _process_exit_summary(str(meta.get("status") or status), meta.get("exitCode") if isinstance(meta.get("exitCode"), int) else None)
    next_record = {
        **_build_task_record(next_meta, summary),
        **record,
        "status": status,
        "summary": record.get("summary") if record.get("status") == status and record.get("summary") else summary,
        "updatedAt": next_meta["updatedAt"],
        "completedAt": meta.get("finishedAt") or meta.get("completedAt") or next_meta["updatedAt"],
        "owner": "daemon",
        "supervisorId": supervisor_id,
        "metadata": {**dict(record.get("metadata") or {}), **_process_state_metadata(process_state)},
    }
    await writeTaskMeta(task_id, next_meta)
    await saveTaskRecord(next_record)
    await maybe_await(emit(_event_name_for_task_status(status), _task_record_event_payload(next_record), record.get("sessionId")))


async def _watch_recovered_process(
    task_id: str,
    pid: int,
    session_id: str | None,
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> None:
    while _is_process_alive(pid):
        await asyncio.sleep(1)
    existing = await loadTaskRecord(task_id)
    if existing and existing.get("kind") == "subagent" and await _recover_completed_subagent_task(task_id, existing, emit, session_id):
        return
    now = now_iso()
    summary = "Recovered daemon-owned task exited after restart. Final completion details were unavailable."
    next_record = {
        **(existing or {"id": task_id, "sessionId": session_id, "kind": "background_command"}),
        "status": "stopped",
        "summary": summary,
        "updatedAt": now,
        "completedAt": now,
    }
    await saveTaskRecord(next_record)
    meta = await readTaskMeta(task_id) or {}
    await writeTaskMeta(task_id, {**meta, "status": "terminated", "updatedAt": now, "finishedAt": now})
    await maybe_await(emit("task.updated", _task_record_event_payload(next_record), session_id))


async def _recover_completed_subagent_task(
    task_id: str,
    record: dict[str, Any],
    emit: Callable[[str, dict[str, Any], str | None], Any],
    session_id: str | None,
) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    agent_id = metadata.get("agent_id") or record.get("sourceId")
    child = await loadChildAgent(agent_id) if isinstance(agent_id, str) and agent_id else None
    if not child or _is_active_child_agent_status(child.get("status")):
        return False
    next_record = _build_recovered_subagent_task_record(record, child)
    await saveTaskRecord(next_record)
    meta = await readTaskMeta(task_id) or {}
    task_meta = {
        **meta,
        "type": "subagent",
        "task_id": task_id,
        "agent_id": child.get("agentId") or agent_id,
        "session_id": child.get("sessionId"),
        "parent_session_id": child.get("parentSessionId") or meta.get("parent_session_id"),
        "parent_tool_use_id": child.get("parentToolUseId") or meta.get("parent_tool_use_id"),
        "parent_turn_id": child.get("parentTurnId") or meta.get("parent_turn_id"),
        "launch_group_id": child.get("launchGroupId") or meta.get("launch_group_id"),
        "status": next_record.get("status"),
        "updatedAt": next_record.get("updatedAt"),
        "finishedAt": next_record.get("completedAt"),
        "summary": next_record.get("summary"),
        "resultSummary": next_record.get("resultSummary"),
        "completionReason": child.get("completionReason"),
        "waitingReason": child.get("waitingReason"),
        "attachableSessionId": child.get("attachableSessionId") or child.get("sessionId"),
        "retryable": child.get("retryable"),
        "stoppable": child.get("stoppable"),
        "owner": "daemon",
    }
    await writeTaskMeta(task_id, task_meta)
    updated_at = next_record.get("updatedAt") or now_iso()
    updated_child = await mutateChildAgent(
        agent_id,
        lambda current: {**(current or child), "recoveryState": "completed_while_detached", "lastUpdatedAt": updated_at},
    )
    reconciliation = await createSubagentReconciliationForTerminalChild(
        childRecord=updated_child or child,
        taskMeta=task_meta,
        source="supervisor_recovery",
    )
    await maybe_await(emit(_event_name_for_task_status(next_record.get("status")), _task_record_event_payload(next_record), session_id or next_record.get("sessionId")))
    if reconciliation:
        await maybe_await(
            emit(
                "subagent.reconciliation.created",
                {
                    "taskId": task_id,
                    "agentId": agent_id,
                    "parentSessionId": reconciliation.get("parentSessionId"),
                    "reconciliationId": reconciliation.get("id"),
                    "launchGroupId": reconciliation.get("launchGroupId"),
                    "terminalStatus": reconciliation.get("terminalStatus"),
                },
                reconciliation.get("parentSessionId"),
            )
        )
    return True


async def _mark_subagent_cancelled_from_stop(meta: dict[str, Any], task_id: str, stopped_at: str) -> None:
    agent_id = meta.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return
    summary = "Stopped by request."

    def _cancel(child: dict[str, Any] | None) -> dict[str, Any] | None:
        if not child:
            return None
        return {
            **child,
            "status": "cancelled",
            "completedAt": stopped_at,
            "taskId": task_id,
            "error": summary,
            "completionReason": "cancelled",
            "resultSummary": summary,
            "latestResultAt": stopped_at,
            "retryable": True,
            "stoppable": False,
            "recoveryState": None,
            "lastUpdatedAt": stopped_at,
            "lastSummary": summary,
        }

    child = await mutateChildAgent(agent_id, _cancel)
    task_meta = {
        **meta,
        "type": "subagent",
        "task_id": task_id,
        "agent_id": agent_id,
        "session_id": (child or {}).get("sessionId") or meta.get("session_id") or meta.get("sessionId"),
        "parent_session_id": (child or {}).get("parentSessionId") or meta.get("parent_session_id"),
        "parent_tool_use_id": (child or {}).get("parentToolUseId") or meta.get("parent_tool_use_id"),
        "parent_turn_id": (child or {}).get("parentTurnId") or meta.get("parent_turn_id"),
        "launch_group_id": (child or {}).get("launchGroupId") or meta.get("launch_group_id"),
        "status": "cancelled",
        "completedAt": stopped_at,
        "summary": summary,
        "resultSummary": summary,
        "completionReason": "cancelled",
        "outputFile": meta.get("outputFile") or taskOutputPath(task_id),
    }
    await createSubagentReconciliationForTerminalChild(
        childRecord=child,
        taskMeta=task_meta,
        error=summary,
        source="supervisor_stop",
    )


def _classify_persisted_process(task_id: str, record: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    status = meta.get("status")
    if _is_terminal_worker_status(status):
        mapped = _map_status(status)
        return {
            "state": "completed" if mapped == "completed" else "failed",
            "pid": _extract_pid(record, meta),
            "source": "terminal_meta",
            "status": status,
        }
    proc = _PROCESS_REGISTRY.get(task_id)
    if proc and proc.poll() is None:
        return {"state": "running", "pid": proc.pid, "source": "process_registry", "status": status}
    pid = _extract_pid(record, meta)
    if isinstance(pid, int):
        if _is_process_alive(pid):
            return {"state": "orphaned", "pid": pid, "source": "pid_probe", "status": status}
        return {"state": "failed", "pid": pid, "source": "pid_probe", "status": status}
    return {"state": "unknown", "pid": None, "source": "missing_pid", "status": status}


def _extract_pid(record: dict[str, Any], meta: dict[str, Any]) -> int | None:
    candidates = [meta.get("pid")]
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    candidates.append(metadata.get("pid"))
    for candidate in candidates:
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    return None


def _process_state_metadata(process_state: dict[str, Any] | None) -> dict[str, Any]:
    if not process_state:
        return {}
    result = {
        "process_state": process_state.get("state"),
        "process_state_source": process_state.get("source"),
    }
    if isinstance(process_state.get("pid"), int):
        result["process_pid"] = process_state["pid"]
    if process_state.get("status") is not None:
        result["process_meta_status"] = process_state.get("status")
    return result
