from __future__ import annotations

from ..._utils import field as _field

import copy
from typing import Any


ACTIVE_TASK_STATUSES = {"queued", "running", "backgrounded", "awaiting_input", "reconciling", "degraded"}

def summarizeSessionRecord(session: dict[str, Any]) -> dict[str, Any]:
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    return {
        "id": _field(session, "id", "sessionId", "session_id"),
        "cwd": _field(session, "cwd"),
        "name": _field(session, "name"),
        "provider": _field(session, "provider"),
        "model": _field(session, "model"),
        "permissionMode": _field(session, "permissionMode", "permission_mode"),
        "updatedAt": _field(session, "updatedAt", "updated_at"),
        "workspaceName": _field(session, "workspaceName", "workspace_name") or metadata.get("workspaceName"),
        "gitRoot": _field(session, "gitRoot", "git_root") or metadata.get("gitRoot"),
        "gitBranch": _field(session, "gitBranch", "git_branch") or metadata.get("gitBranch"),
        "remoteSessionId": _field(session, "remoteSessionId", "remote_session_id") or metadata.get("remoteSessionId"),
        "bridgeId": _field(session, "bridgeId", "bridge_id") or metadata.get("bridgeId"),
    }


def _extract_last_assistant_text(session: dict[str, Any]) -> str:
    for message in reversed(session.get("messages") or []):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text").strip()
    return ""

def _clear_runtime_live_status(state: dict[str, Any]) -> dict[str, Any]:
    if (state.get("turn", {}).get("liveStatus") or {}).get("source") != "runtime":
        return state
    state["turn"] = {**state.get("turn", {}), "liveStatus": None}
    return state

def _task_from_snapshot_record(task: dict[str, Any], active_session_id: str | None, timestamp: str) -> dict[str, Any]:
    task_id = _field(task, "id", "taskId", "task_id")
    updated_at = _field(task, "updatedAt", "updated_at") or timestamp
    return {
        "id": task_id,
        "sessionId": _field(task, "sessionId", "session_id") or active_session_id or "",
        "sourceId": task_id,
        "kind": _field(task, "kind") or "background_command",
        "status": _field(task, "status") or "idle",
        "title": _field(task, "title") or task_id,
        "summary": _field(task, "summary"),
        "createdAt": updated_at,
        "updatedAt": updated_at,
        "startedAt": updated_at,
        "foregroundable": True,
        "restorable": True,
        "foregroundMode": _field(task, "foregroundMode", "foreground_mode"),
        "taskFamily": _field(task, "taskFamily", "task_family"),
        "detailStatus": _field(task, "detailStatus", "detail_status"),
        "attachableSessionId": _field(task, "attachableSessionId", "attachable_session_id"),
        "retrieveOnly": _field(task, "retrieveOnly", "retrieve_only"),
        "resultSummary": _field(task, "resultSummary", "result_summary"),
    }

def _task_from_event(data: dict[str, Any], task_id: str, status: str, active_session_id: str | None, timestamp: str, *, created: bool) -> dict[str, Any]:
    return {
        "id": task_id,
        "sessionId": _field(data, "sessionId", "session_id") or active_session_id or "",
        "sourceId": task_id,
        "kind": _field(data, "kind") or "background_command",
        "status": status,
        "title": _field(data, "title") or task_id,
        "summary": _field(data, "summary"),
        "createdAt": timestamp if created else _field(data, "createdAt", "created_at") or timestamp,
        "updatedAt": timestamp,
        "startedAt": timestamp,
        "foregroundable": True,
        "restorable": True,
        "foregroundMode": _field(data, "foregroundMode", "foreground_mode"),
        "taskFamily": _field(data, "taskFamily", "task_family"),
        "detailStatus": _field(data, "detailStatus", "detail_status"),
        "attachableSessionId": _field(data, "attachableSessionId", "attachable_session_id"),
        "retrieveOnly": _field(data, "retrieveOnly", "retrieve_only"),
        "resultSummary": _field(data, "resultSummary", "result_summary"),
    }

def _task_patch_from_event(data: dict[str, Any], status: str | None, timestamp: str) -> dict[str, Any]:
    return {
        "kind": _field(data, "kind"),
        "status": status,
        "title": _field(data, "title"),
        "summary": _field(data, "summary"),
        "foregroundMode": _field(data, "foregroundMode", "foreground_mode"),
        "taskFamily": _field(data, "taskFamily", "task_family"),
        "detailStatus": _field(data, "detailStatus", "detail_status"),
        "attachableSessionId": _field(data, "attachableSessionId", "attachable_session_id"),
        "retrieveOnly": _field(data, "retrieveOnly", "retrieve_only"),
        "resultSummary": _field(data, "resultSummary", "result_summary"),
        "updatedAt": timestamp,
    }

def _upsert_runtime_task(state: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    task_slice = copy.deepcopy(state.get("tasks") or {})
    tasks = [copy.deepcopy(existing) for existing in task_slice.get("tasks", [])]
    index = next((idx for idx, existing in enumerate(tasks) if existing.get("id") == task.get("id")), -1)
    if index >= 0:
        tasks[index] = {**tasks[index], **{key: value for key, value in task.items() if value is not None}}
    else:
        tasks.insert(0, {key: value for key, value in task.items() if value is not None})
    state["tasks"] = _summarize_tasks({**task_slice, "tasks": tasks, "lastUpdatedAt": task.get("updatedAt")})
    return state

def _update_runtime_task(state: dict[str, Any], task_id: str | None, patch: dict[str, Any]) -> dict[str, Any]:
    if not task_id:
        return state
    task_slice = state.get("tasks") or {}
    existing = next((task for task in task_slice.get("tasks", []) if task.get("id") == task_id), None)
    if not existing:
        return state
    merged = {**existing, **{key: value for key, value in patch.items() if value is not None}}
    return _upsert_runtime_task(state, merged)

def _summarize_tasks(task_slice: dict[str, Any]) -> dict[str, Any]:
    tasks = [copy.deepcopy(task) for task in task_slice.get("tasks", [])]
    tasks.sort(key=lambda task: str(task.get("updatedAt") or ""), reverse=True)
    tasks = tasks[:24]
    active = [task for task in tasks if task.get("status") in ACTIVE_TASK_STATUSES]
    labels = [task.get("title") for task in active[:6] if task.get("title")]
    return {
        **task_slice,
        "total": max(int(task_slice.get("total") or 0), len(tasks)),
        "tasks": tasks,
        "activeCount": len(active),
        "backgroundedCount": _count_tasks(tasks, "backgrounded"),
        "awaitingInputCount": _count_tasks(tasks, "awaiting_input"),
        "failedCount": sum(1 for task in tasks if task.get("status") in {"failed", "degraded"}),
        "labels": labels,
    }

def _count_tasks(tasks: list[dict[str, Any]], status: str) -> int:
    return sum(1 for task in tasks if task.get("status") == status)

def _collaboration_worker_record(worker: dict[str, Any], timestamp: str) -> dict[str, Any]:
    return {
        "id": _field(worker, "id"),
        "kind": _field(worker, "kind") or "monitor",
        "status": _field(worker, "status") or "idle",
        "label": _field(worker, "label") or _field(worker, "id"),
        "summary": _field(worker, "summary"),
        "sessionId": _field(worker, "sessionId", "session_id"),
        "parentSessionId": _field(worker, "parentSessionId", "parent_session_id"),
        "parentTaskId": _field(worker, "parentTaskId", "parent_task_id"),
        "childSessionId": _field(worker, "childSessionId", "child_session_id"),
        "taskId": _field(worker, "taskId", "task_id"),
        "monitorId": _field(worker, "monitorId", "monitor_id"),
        "remoteSessionId": _field(worker, "remoteSessionId", "remote_session_id"),
        "bridgeId": _field(worker, "bridgeId", "bridge_id"),
        "attachableSessionId": _field(worker, "attachableSessionId", "attachable_session_id"),
        "owner": _field(worker, "owner") if _field(worker, "owner") in {"daemon", "client"} else None,
        "waitingReason": _field(worker, "waitingReason", "waiting_reason"),
        "completionReason": _field(worker, "completionReason", "completion_reason"),
        "resultSummary": _field(worker, "resultSummary", "result_summary"),
        "latestResultAt": _field(worker, "latestResultAt", "latest_result_at"),
        "retryable": _field(worker, "retryable"),
        "stoppable": _field(worker, "stoppable"),
        "recoveryState": _field(worker, "recoveryState", "recovery_state"),
        "updatedAt": _field(worker, "updatedAt", "updated_at") or timestamp,
    }

def _unique_push(values: list[str], next_value: str) -> list[str]:
    return values if next_value in values else [*values, next_value]

def _format_compaction_live_detail(trigger: str | None, current_token_estimate: int | None) -> str | None:
    parts = [part for part in [trigger.strip() if isinstance(trigger, str) else None, f"{current_token_estimate} tokens" if isinstance(current_token_estimate, int) else None] if part]
    return " | ".join(parts) if parts else None

def _default_tool_status_message(name: str) -> str:
    if name == "Read":
        return "Reading files..."
    if name in {"Write", "Edit", "NotebookEdit"}:
        return "Editing files..."
    if name == "Bash":
        return "Running command..."
    if name in {"WebFetch", "WebSearch"}:
        return "Checking the web..."
    if name == "TaskOutput":
        return "Checking task output..."
    return f"{name} running..."

def _default_tool_status_detail(name: str) -> str | None:
    if name == "TaskMonitorStart":
        return "starting background monitor"
    if name == "TaskMonitorStop":
        return "stopping background monitor"
    return None

def _default_outcome_message(stop_reason: str | None) -> str:
    if stop_reason == "plan_approval_required":
        return "Plan ready for review."
    if stop_reason == "plan_chat_requested":
        return "Still in plan mode. Awaiting your reply."
    if stop_reason == "plan_rejected":
        return "Plan rejected."
    if stop_reason == "max_tokens":
        return "Response truncated. Awaiting your next prompt."
    if stop_reason == "max_budget_exceeded":
        return "Budget limit reached."
    return "Turn complete. Ready for the next prompt."

def _session_title_from_summary(workspace_name: str | None, remote_session_id: str | None) -> str | None:
    if workspace_name:
        return workspace_name
    return f"remote {remote_session_id[:8]}" if remote_session_id else None

def _normalize_pressure_level(value: str | None) -> str | None:
    return value if value in {"low", "elevated", "high", "critical"} else None

def _normalize_daemon_phase(value: str | None) -> str | None:
    return value if value in {"starting", "recovering", "running", "shutting_down"} else None
