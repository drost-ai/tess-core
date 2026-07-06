from __future__ import annotations

from .shared import *


async def _project_session_task_records(session_id: str, session: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    session = session or await loadSession(session_id) or {"id": session_id, "metadata": {}}
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    records_by_id: dict[str, dict[str, Any]] = {}

    monitors = await listSessionMonitors(session_id)
    task_ids = list(metadata.get("taskIds") or [])
    task_ids.extend(monitor.get("taskId") for monitor in monitors if monitor.get("taskId"))
    task_ids.extend(task.get("taskId") for task in await listHealthCheckTasks() if task.get("sessionId") == session_id and task.get("taskId"))
    for task_id in _dedupe([item for item in task_ids if isinstance(item, str) and item]):
        try:
            meta = await readTaskMeta(task_id)
            if meta:
                record = _build_task_record(meta, None, session)
                related_monitor_ids = [monitor.get("id") for monitor in monitors if monitor.get("taskId") == task_id and monitor.get("id")]
                if related_monitor_ids:
                    record["relatedMonitorIds"] = related_monitor_ids
                records_by_id[task_id] = record
        except Exception:
            continue

    for monitor in monitors:
        monitor_id = monitor.get("id")
        if monitor_id:
            try:
                records_by_id[f"monitor:{monitor_id}"] = _build_monitor_task_record(session, monitor)
            except Exception:
                continue

    for child in await listChildAgents():
        if _is_related_child_agent(session, child):
            try:
                child_record = await _build_child_agent_task_record(session, child)
                if child_record.get("id"):
                    records_by_id[str(child_record["id"])] = child_record
            except Exception:
                continue

    remote_id = metadata.get("remoteSessionId")
    remote = await loadRemoteSession(remote_id) if isinstance(remote_id, str) and remote_id else None
    if remote:
        try:
            bridge = await loadBridge(remote.get("bridgeId")) if isinstance(remote.get("bridgeId"), str) else None
            records_by_id[f"remote:{remote['id']}"] = _build_remote_task_record(session, remote, (bridge or {}).get("status"))
        except Exception:
            pass

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
    task_ids = [
        record.get("id")
        for record in records
        if record.get("kind") in {"background_command", "main_session", "health_check"} and record.get("id")
    ]
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


def _subscription_session_id(value: str | dict[str, Any] | None) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        maybe = value.get("sessionId") or value.get("session_id")
        return maybe if isinstance(maybe, str) and maybe else None
    return None


def _event_matches_session(payload: dict[str, Any], session_id: str | None) -> bool:
    if not session_id:
        return True
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("sessionId") == session_id or data.get("sessionId") == session_id or data.get("session_id") == session_id
