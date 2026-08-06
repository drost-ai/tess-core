from __future__ import annotations

from ...daemon.events import _event_matches_session

from ..._utils import now_iso
from ...session.persisted_record_scanner import (
    getPersistedRecordIssue,
    listPersistedRecordIssues,
)
from ...storage import (
    getStateRoot,
    hasPersistableSessionMessages,
    listChildAgents,
    listHealthCheckTasks,
    listSessionMonitors,
    loadBridge,
    loadRemoteSession,
    loadSession,
    loadTaskRecord,
    readTaskMeta,
    saveSession,
    saveTaskRecord,
    taskMetaPath,
    writeTaskMeta,
)
from .live_work import _resolve_session_live_work
from .mappers import _is_related_child_agent
from .records import (
    _build_child_agent_task_record,
    _build_monitor_task_record,
    _build_remote_task_record,
    _build_task_record,
)
from .utils import (
    _bucket_ms,
    _dedupe,
    _parse_iso_ms,
    _summarize_goal,
)
from typing import Any, Callable


async def _project_session_task_records(
    session_id: str,
    session: dict[str, Any] | None = None,
    live_work_cache: dict[str, Any] | None = None,
    on_issue: Callable[[dict[str, Any]], Any] | None = None,
) -> list[dict[str, Any]]:
    session = session or await loadSession(session_id) or {"id": session_id, "metadata": {}}
    # Projection is also the existing restoration/attachment boundary. Verify
    # marker authority first so dead records converge before snapshots are
    # built; the verdict itself never reads the session transcript archive.
    if live_work_cache:
        await live_work_cache["resolve"](session)
    else:
        await _resolve_session_live_work(session)
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    records_by_id: dict[str, dict[str, Any]] = {}

    monitors = await listSessionMonitors(session_id)
    child_agents = await listChildAgents()
    health_checks = await listHealthCheckTasks()
    for issue in listPersistedRecordIssues(
        ("agent", "health_check", "monitor"),
        getStateRoot(),
    ):
        if on_issue:
            on_issue(_projection_issue(issue))
    task_ids = list(metadata.get("taskIds") or [])
    task_ids.extend(monitor.get("taskId") for monitor in monitors if monitor.get("taskId"))
    task_ids.extend(task.get("taskId") for task in health_checks if task.get("sessionId") == session_id and task.get("taskId"))
    for task_id in _dedupe([item for item in task_ids if isinstance(item, str) and item]):
        try:
            meta = await readTaskMeta(task_id)
            if not meta:
                issue = getPersistedRecordIssue(taskMetaPath(task_id))
                if on_issue:
                    on_issue(
                        _projection_issue(issue, task_id)
                        if issue
                        else {"taskId": task_id, "reason": "referenced task meta is missing", "unavailable": True}
                    )
                continue
            task_type = meta.get("type") or ""
            if task_type not in {"", "background_command", "main_session", "subagent", "health_check"}:
                if on_issue:
                    on_issue({"taskId": task_id, "reason": f'unknown task meta type "{task_type}"'})
                continue
            if task_type == "subagent" and any(
                child.get("taskId") == task_id and _is_related_child_agent(session, child)
                for child in child_agents
            ):
                continue
            if meta:
                record = _build_task_record(meta, None, session)
                related_monitor_ids = [monitor.get("id") for monitor in monitors if monitor.get("taskId") == task_id and monitor.get("id")]
                if related_monitor_ids:
                    record["relatedMonitorIds"] = related_monitor_ids
                records_by_id[task_id] = record
        except Exception as exc:
            if on_issue:
                on_issue({"taskId": task_id, "reason": str(exc)})

    for monitor in monitors:
        monitor_id = monitor.get("id")
        if monitor_id:
            try:
                records_by_id[f"monitor:{monitor_id}"] = _build_monitor_task_record(session, monitor)
            except Exception as exc:
                if on_issue:
                    on_issue({"taskId": monitor.get("taskId"), "reason": str(exc)})

    for child in child_agents:
        if _is_related_child_agent(session, child):
            try:
                child_record = await _build_child_agent_task_record(session, child)
                if child_record.get("id"):
                    records_by_id[str(child_record["id"])] = child_record
            except Exception as exc:
                if on_issue:
                    on_issue({"taskId": child.get("taskId") or child.get("agentId"), "reason": str(exc)})

    remote_id = metadata.get("remoteSessionId")
    remote = await loadRemoteSession(remote_id) if isinstance(remote_id, str) and remote_id else None
    if remote:
        try:
            bridge = await loadBridge(remote.get("bridgeId")) if isinstance(remote.get("bridgeId"), str) else None
            records_by_id[f"remote:{remote['id']}"] = _build_remote_task_record(session, remote, (bridge or {}).get("status"))
        except Exception as exc:
            if on_issue:
                on_issue({"reason": str(exc)})

    records = sorted(records_by_id.values(), key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
    await _sync_projected_task_metadata(session, records)
    return records


async def _annotate_retry_lineage(original_task_id: str, new_task_id: Any, source: dict[str, Any]) -> None:
    if not isinstance(new_task_id, str) or not new_task_id:
        return
    source_metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    root_task_id = (
        source_metadata.get("retry_root_task_id")
        or source_metadata.get("retryRootTaskId")
        or source.get("retryRootTaskId")
        or original_task_id
    )
    previous_attempt = source_metadata.get("retry_attempt", source_metadata.get("retryAttempt", source.get("retryAttempt", 0)))
    try:
        retry_attempt = int(previous_attempt) + 1
    except Exception:
        retry_attempt = 1
    lineage = {
        "retry_of_task_id": original_task_id,
        "retry_root_task_id": root_task_id,
        "retry_attempt": retry_attempt,
    }
    camel_lineage = {
        "retryOfTaskId": original_task_id,
        "retryRootTaskId": root_task_id,
        "retryAttempt": retry_attempt,
    }
    meta = await readTaskMeta(new_task_id)
    if meta:
        meta_metadata = dict(meta.get("metadata") or {})
        meta_metadata.update(lineage)
        await writeTaskMeta(new_task_id, {**meta, **camel_lineage, "metadata": meta_metadata})
    record = await loadTaskRecord(new_task_id)
    if record:
        record_metadata = dict(record.get("metadata") or {})
        record_metadata.update(lineage)
        await saveTaskRecord({**record, "metadata": record_metadata})


async def _sync_projected_task_metadata(session: dict[str, Any], records: list[dict[str, Any]]) -> None:
    if not session.get("id"):
        return
    metadata = session.setdefault("metadata", {})
    projected_task_ids = [
        record.get("id")
        for record in records
        if record.get("kind") in {"background_command", "main_session", "health_check"} and record.get("id")
    ]
    task_ids = _dedupe([*(metadata.get("taskIds") or []), *projected_task_ids])
    restore = {
        "savedAt": now_iso(),
        "activeTasks": [
            {
                "id": record.get("id"),
                "kind": record.get("kind"),
                "status": record.get("status"),
                "title": record.get("title"),
                "summary": record.get("summary"),
                "updatedAt": record.get("updatedAt"),
                "foregroundMode": record.get("foregroundMode"),
                "taskFamily": record.get("taskFamily"),
                "detailStatus": record.get("detailStatus"),
                "attachableSessionId": record.get("attachableSessionId"),
                "retrieveOnly": record.get("retrieveOnly"),
                "resultSummary": record.get("resultSummary"),
            }
            for record in records
            if _is_active_task_status(record.get("status"))
        ][:12],
        "restoredTaskCount": sum(1 for record in records if record.get("restorable")),
        "lastTaskEventAt": records[0].get("updatedAt") if records else None,
    }
    previous_task_ids = list(metadata.get("taskIds") or [])
    previous_restore = metadata.get("runtimeRestore") or {}
    if previous_task_ids == task_ids and _restore_without_saved_at(previous_restore) == _restore_without_saved_at(restore):
        return
    metadata["taskIds"] = task_ids
    metadata["runtimeRestore"] = restore
    # Observer projection is bookkeeping, not semantic session activity.
    # Import locally to avoid a package initialization cycle.
    from ...session.persistence import persistSession

    if hasPersistableSessionMessages(session):
        await persistSession(session, {"sessionPersistence": True}, touch=False)
    else:
        # Empty child/bootstrap sessions still need their restore projection,
        # and saveSession already preserves their semantic timestamp.
        await saveSession(session)


def _task_snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = records[:16]
    return {
        "total": len(records),
        "active": sum(1 for record in records if _is_active_task_status(record.get("status"))),
        "backgrounded": sum(1 for record in records if record.get("status") == "backgrounded"),
        "awaitingInput": sum(1 for record in records if record.get("status") == "awaiting_input"),
        "failed": sum(1 for record in records if record.get("status") == "failed"),
        "labels": [record.get("title") for record in tasks if _is_active_task_status(record.get("status")) and record.get("title")][:6],
        "tasks": tasks,
    }


def _monitor_snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    active = [record for record in records if record.get("status") == "active"]
    next_check_in_ms = None
    if active:
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000
        candidates = []
        for record in active:
            last_check = _parse_iso_ms(record.get("lastCheckAt") or record.get("lastRunAt")) or 0
            candidates.append(max(0, last_check + int(record.get("checkIntervalMs") or 5000) - now))
        next_check_in_ms = _bucket_ms(min(candidates))
    return {
        "total": len(records),
        "active": len(active),
        "failed": sum(1 for record in records if record.get("status") == "failed"),
        "stopped": sum(1 for record in records if record.get("status") == "stopped"),
        "muted": sum(1 for record in records if record.get("status") == "muted"),
        "nextCheckInMs": next_check_in_ms,
        "labels": [_summarize_goal(record.get("goal", "")) for record in active[:3]],
    }


def _empty_collaboration_snapshot() -> dict[str, Any]:
    return {
        "workers": [],
        "activeCount": 0,
        "awaitingInputCount": 0,
        "backgroundCount": 0,
        "labels": [],
    }


async def _collaboration_snapshot(session_id: str) -> dict[str, Any]:
    session = await loadSession(session_id)
    if not session:
        return _empty_collaboration_snapshot()
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    remote_id = metadata.get("remoteSessionId")
    remote = await loadRemoteSession(remote_id) if isinstance(remote_id, str) and remote_id else None
    bridge_id = metadata.get("bridgeId") or (remote or {}).get("bridgeId")
    bridge = await loadBridge(bridge_id) if isinstance(bridge_id, str) and bridge_id else None
    from ...collaboration import buildCollaborationWorkers, createCollaborationSnapshot

    workers = buildCollaborationWorkers(
        {
            "session": session,
            "childAgents": await listChildAgents(),
            "monitors": await listSessionMonitors(session_id),
            "remote": remote,
            "bridge": bridge,
        }
    )
    return createCollaborationSnapshot(workers, remote, bridge)


def _is_active_task_status(status: Any) -> bool:
    return status in {"queued", "running", "backgrounded", "awaiting_input", "reconciling", "degraded"}


def _restore_without_saved_at(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key != "savedAt"}


def _projection_issue(issue: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
    return {
        "taskId": task_id if task_id is not None else issue.get("id"),
        "reason": issue.get("reason"),
        "family": issue.get("family"),
        "filePath": issue.get("filePath"),
        "size": issue.get("size"),
        "unavailable": issue.get("unavailable", True),
    }


def _subscription_session_id(value: str | dict[str, Any] | None) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        maybe = value.get("sessionId") or value.get("session_id")
        return maybe if isinstance(maybe, str) and maybe else None
    return None


