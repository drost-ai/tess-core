from __future__ import annotations

import uuid
from typing import Any


def resolveSubagentLaunchGroupId(args: dict[str, Any], parentSession: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    context = context or {}
    for value in (
        args.get("launchGroupId"),
        args.get("launch_group_id"),
        context.get("subagentLaunchGroupId"),
        context.get("parentTurnId"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = parentSession.get("metadata") if isinstance(parentSession.get("metadata"), dict) else {}
    active = metadata.get("activeSubagentLaunchGroupId")
    if isinstance(active, str) and active.strip():
        return active.strip()
    return f"session-{parentSession.get('id') or uuid.uuid4()}"


def appendLaunchGroupMetadata(parentSession: dict[str, Any], launchGroupId: str, childAgentId: str, taskId: str | None = None) -> None:
    metadata = parentSession.setdefault("metadata", {})
    groups = metadata.setdefault("subagentLaunchGroups", [])
    if launchGroupId and launchGroupId not in groups:
        groups.append(launchGroupId)
    group_records = metadata.setdefault("subagentLaunchGroupRecords", {})
    if isinstance(group_records, dict) and launchGroupId:
        current = group_records.get(launchGroupId) if isinstance(group_records.get(launchGroupId), dict) else {}
        child_ids = _dedupe([*(current.get("childAgentIds") or []), childAgentId])
        task_ids = _dedupe([*(current.get("taskIds") or []), taskId] if taskId else (current.get("taskIds") or []))
        group_records[launchGroupId] = {
            **current,
            "id": launchGroupId,
            "parentSessionId": parentSession.get("id"),
            "childAgentIds": child_ids,
            "taskIds": task_ids,
        }


def _dedupe(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


resolve_subagent_launch_group_id = resolveSubagentLaunchGroupId
append_launch_group_metadata = appendLaunchGroupMetadata
