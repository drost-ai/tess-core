from __future__ import annotations

from typing import Any, Mapping

from ..runtime.identifiers import parseMonitorId, parseTaskId
from ..storage import loadChildAgent, loadMonitor, loadTaskRecord, readTaskMeta


class TaskOwnershipError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_ALLOWED_KINDS = {
    "output": {"background_command", "main_session", "subagent", "health_check"},
    "stop": {"background_command", "main_session", "subagent", "health_check"},
    "monitor": {"background_command", "main_session", "subagent"},
    "health_stop": {"health_check"},
}


async def requireModelOwnedTask(context: Mapping[str, Any], rawTaskId: str, operation: str) -> dict[str, Any]:
    task_id = parseTaskId(rawTaskId)
    record = await loadTaskRecord(task_id)
    meta = await readTaskMeta(task_id)
    if not record and not meta:
        raise TaskOwnershipError("task.not_found", f"Task not found: {task_id}")
    _assert_record_identity(task_id, record, meta)
    kind = _resolve_task_kind(record, meta)
    if operation not in _ALLOWED_KINDS or kind not in _ALLOWED_KINDS[operation]:
        raise TaskOwnershipError(
            "task.kind_mismatch",
            f"Task {task_id} does not support {operation.replace('_', ' ')}.",
        )
    session = context.get("session")
    if not isinstance(session, Mapping) or not isinstance(session.get("id"), str):
        raise TaskOwnershipError("task.not_owned", f"Task not found in the active session: {task_id}")
    child = await _load_related_child(meta, record) if kind == "subagent" else None
    if not _belongs_to_session(session["id"], kind, record, meta, child):
        raise TaskOwnershipError("task.not_owned", f"Task not found in the active session: {task_id}")
    if not _is_referenced_by_session(session, task_id, kind, record, meta, child):
        raise TaskOwnershipError(
            "task.not_referenced",
            f"Task is not registered to the active session: {task_id}",
        )
    return {"id": task_id, "kind": kind, "record": record, "meta": meta or {}}


async def requireModelOwnedMonitor(context: Mapping[str, Any], rawMonitorId: str) -> dict[str, Any]:
    monitor_id = parseMonitorId(rawMonitorId)
    monitor = await loadMonitor(monitor_id)
    session = context.get("session")
    if (
        not isinstance(monitor, dict)
        or monitor.get("id") != monitor_id
        or not isinstance(session, Mapping)
        or monitor.get("sessionId") != session.get("id")
    ):
        raise TaskOwnershipError(
            "task.not_owned",
            f"Monitor not found in the active session: {monitor_id}",
        )
    metadata = session.get("metadata") if isinstance(session.get("metadata"), Mapping) else {}
    if monitor_id not in (metadata.get("monitorIds") or []):
        raise TaskOwnershipError(
            "task.not_referenced",
            f"Monitor is not registered to the active session: {monitor_id}",
        )
    return monitor


def _assert_record_identity(task_id: str, record: Any, meta: Any) -> None:
    if isinstance(record, Mapping) and (
        record.get("id") != task_id
        or not isinstance(record.get("sourceId"), str)
        or not record.get("sourceId")
    ):
        raise TaskOwnershipError("task.record_mismatch", f"Task record identity mismatch for {task_id}.")
    if isinstance(meta, Mapping):
        for field in ("taskId", "task_id"):
            value = meta.get(field)
            if isinstance(value, str) and value != task_id:
                raise TaskOwnershipError("task.record_mismatch", f"Task metadata identity mismatch for {task_id}.")
    record_kind = record.get("kind") if isinstance(record, Mapping) else None
    meta_kind = _normalize_meta_kind(meta.get("type") if isinstance(meta, Mapping) else None)
    if record_kind and meta_kind and record_kind != meta_kind:
        raise TaskOwnershipError("task.record_mismatch", f"Task family mismatch for {task_id}.")


def _resolve_task_kind(record: Any, meta: Any) -> str | None:
    return (record.get("kind") if isinstance(record, Mapping) else None) or _normalize_meta_kind(
        meta.get("type") if isinstance(meta, Mapping) else None
    )


def _normalize_meta_kind(value: Any) -> str | None:
    if value in {None, ""}:
        return "background_command"
    return value if value in {"background_command", "main_session", "subagent", "health_check", "monitor", "remote"} else None


async def _load_related_child(meta: Any, record: Any) -> dict[str, Any] | None:
    metadata = record.get("metadata") if isinstance(record, Mapping) and isinstance(record.get("metadata"), Mapping) else {}
    agent_id = (
        _string_field(meta, "agent_id")
        or _string_field(metadata, "agent_id")
        or (record.get("sourceId") if isinstance(record, Mapping) else None)
    )
    return await loadChildAgent(agent_id) if isinstance(agent_id, str) and agent_id else None


def _belongs_to_session(session_id: str, kind: str, record: Any, meta: Any, child: Any) -> bool:
    if isinstance(record, Mapping) and record.get("sessionId") == session_id:
        return True
    if kind == "subagent":
        return bool(
            _string_field(meta, "parent_session_id") == session_id
            or (isinstance(child, Mapping) and child.get("parentSessionId") == session_id)
            or _string_field(meta, "session_id") == session_id
        )
    return bool(_string_field(meta, "sessionId") == session_id or _string_field(meta, "session_id") == session_id)


def _is_referenced_by_session(
    session: Mapping[str, Any],
    task_id: str,
    kind: str,
    record: Any,
    meta: Any,
    child: Any,
) -> bool:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), Mapping) else {}
    if task_id in (metadata.get("taskIds") or []) or metadata.get("subagentTaskId") == task_id:
        return True
    if kind != "subagent":
        return False
    record_metadata = record.get("metadata") if isinstance(record, Mapping) and isinstance(record.get("metadata"), Mapping) else {}
    agent_id = (
        child.get("agentId") if isinstance(child, Mapping) else None
    ) or _string_field(meta, "agent_id") or _string_field(record_metadata, "agent_id")
    child_session_id = (
        child.get("sessionId") if isinstance(child, Mapping) else None
    ) or _string_field(meta, "session_id") or (record.get("attachableSessionId") if isinstance(record, Mapping) else None)
    return bool(
        (agent_id and agent_id in (metadata.get("childAgentIds") or []))
        or (child_session_id and child_session_id in (metadata.get("childSessionIds") or []))
        or (child_session_id and child_session_id in (metadata.get("backgroundSessionIds") or []))
    )


def _string_field(value: Any, field: str) -> str | None:
    candidate = value.get(field) if isinstance(value, Mapping) else None
    return candidate if isinstance(candidate, str) and candidate else None


