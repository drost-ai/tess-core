from __future__ import annotations

from .shared import *


def _task_event_payload(meta: dict[str, Any]) -> dict[str, Any]:
    record = _build_task_record(meta, None)
    return _task_record_event_payload(record)


def _task_record_event_payload(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    payload = {
        "session_id": record.get("sessionId"),
        "task_id": record.get("id"),
        "kind": record.get("kind"),
        "status": record.get("status"),
        "title": record.get("title"),
        "summary": record.get("summary"),
        "detail_status": record.get("detailStatus"),
        "foreground_mode": record.get("foregroundMode"),
        "task_family": record.get("taskFamily"),
        "attachable_session_id": record.get("attachableSessionId"),
        "retrieve_only": record.get("retrieveOnly"),
        "result_summary": record.get("resultSummary"),
        "pending_questions": metadata.get("pending_questions"),
        "structured_result_status": metadata.get("structured_result_status"),
        "runtime_contract": metadata.get("runtime_contract"),
        "transition_id": metadata.get("terminal_transition_id"),
    }
    for key in ("pending_questions", "structured_result_status", "runtime_contract", "transition_id"):
        if payload.get(key) is None:
            payload.pop(key, None)
    return payload


def _map_status(value: Any) -> str:
    if value == "active":
        return "running"
    if value == "unhealthy":
        return "degraded"
    if value in ("running", "completed", "failed", "stopped", "queued", "awaiting_input", "reconciling", "degraded"):
        return str(value)
    if value == "awaiting_plan_approval":
        return "awaiting_input"
    if value == "truncated":
        return "failed"
    if value in ("terminated", "cancelled"):
        return "stopped"
    return "idle"


def _map_monitor_status(status: Any) -> str:
    if status == "active":
        return "running"
    if status in {"completed", "failed"}:
        return str(status)
    if status in {"stopped", "muted"}:
        return "stopped"
    return "idle"


def _map_remote_status(status: Any) -> str:
    if status in {"created", "active"}:
        return "running"
    if status == "idle":
        return "idle"
    if status == "awaiting_input":
        return "awaiting_input"
    if status == "completed":
        return "completed"
    if status in {"failed", "closed"}:
        return "failed"
    return "idle"


def _is_active_child_agent_status(status: Any) -> bool:
    return status in {"queued", "running", "backgrounded"}


def _map_child_agent_status_to_task_status(status: Any) -> str:
    if status in {"queued", "running", "backgrounded", "completed", "failed"}:
        return str(status)
    if status in {"awaiting_input", "awaiting_plan_approval"}:
        return "awaiting_input"
    if status in {"cancelled", "stopped"}:
        return "stopped"
    if status == "truncated":
        return "failed"
    return "idle"


def _is_related_child_agent(session: dict[str, Any], child: dict[str, Any]) -> bool:
    if child.get("parentSessionId") == session.get("id") or child.get("sessionId") == session.get("id"):
        return True
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    child_sessions = set(metadata.get("childSessionIds") or [])
    background_sessions = set(metadata.get("backgroundSessionIds") or [])
    child_agents = set(metadata.get("childAgentIds") or [])
    return child.get("sessionId") in child_sessions or child.get("sessionId") in background_sessions or child.get("agentId") in child_agents


def _event_name_for_task_status(status: Any) -> str:
    if status == "completed":
        return "task.completed"
    if status == "failed":
        return "task.failed"
    if status == "stopped":
        return "task.stopped"
    return "task.updated"


def _is_terminal_worker_status(status: Any) -> bool:
    return status in {
        "completed",
        "failed",
        "terminated",
        "stopped",
        "cancelled",
        "truncated",
        "awaiting_input",
        "awaiting_plan_approval",
    }
