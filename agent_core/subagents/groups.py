from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from .._utils import now_iso
from ..storage import sessionArchiveDir, withRecordTransaction


def resolveSubagentLaunchGroupId(args: dict[str, Any], parentSession: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    context = context or {}
    for value in (
        args.get("launchGroupId"),
        args.get("launch_group_id"),
        context.get("subagentLaunchGroupId"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = parentSession.get("metadata") if isinstance(parentSession.get("metadata"), dict) else {}
    message_count = len(parentSession.get("messages") or [])
    team = args.get("teamName") or args.get("team_name") or context.get("teamName") or metadata.get("teamName") or "default"
    return f"subagent-group-{parentSession.get('id') or uuid.uuid4()}-{message_count}-{_slug(team)}"


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


def _subagent_launch_groups_path(parentSessionId: str) -> str:
    return os.path.join(sessionArchiveDir(parentSessionId), "subagent-launch-groups.json")


async def _read_subagent_launch_groups(parentSessionId: str) -> list[dict[str, Any]]:
    try:
        with open(_subagent_launch_groups_path(parentSessionId), "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


async def _upsert_subagent_launch_group(parentSessionId: str, group: dict[str, Any]) -> dict[str, Any]:
    group_id = _string(group.get("id") or group.get("launchGroupId"))
    if not group_id:
        raise ValueError("Subagent launch group requires an id.")
    now = now_iso()
    path = _subagent_launch_groups_path(parentSessionId)
    async with withRecordTransaction("subagent-coordination", parentSessionId):
        records = await _read_subagent_launch_groups(parentSessionId)
        current = next((record for record in records if record.get("id") == group_id), None)
        merged = _merge_group_record(current or {}, group, parentSessionId, group_id, now)
        next_records = [record for record in records if record.get("id") != group_id]
        next_records.append(merged)
        await _write_subagent_launch_groups(path, _sort_groups(next_records))
    return merged






def _merge_group_record(current: dict[str, Any], group: dict[str, Any], parent_session_id: str, group_id: str, now: str) -> dict[str, Any]:
    child_ids = _dedupe([*(current.get("childAgentIds") or []), *(group.get("childAgentIds") or []), group.get("childAgentId")])
    task_ids = _dedupe([*(current.get("taskIds") or []), *(group.get("taskIds") or []), group.get("taskId")])
    merged = {
        **current,
        **{key: value for key, value in group.items() if value is not None},
        "id": group_id,
        "parentSessionId": parent_session_id,
        "label": _string(group.get("label")) or _string(current.get("label")) or group_id,
        "childAgentIds": child_ids,
        "taskIds": task_ids,
        "createdAt": current.get("createdAt") or group.get("createdAt") or now,
        "updatedAt": now,
    }
    for key in ("continuationRequestedAt", "continuationRunId", "continuationTaskId", "continuationPrompt"):
        if current.get(key) is not None and group.get(key) is None:
            merged[key] = current[key]
    return {key: value for key, value in merged.items() if value is not None}


async def _write_subagent_launch_groups(path: str, records: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def _sort_groups(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda item: (_string(item.get("createdAt")), _string(item.get("id"))))


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


def _slug(value: Any) -> str:
    text = value if isinstance(value, str) else str(value or "")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "default"


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


