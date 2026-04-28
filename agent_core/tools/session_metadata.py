from __future__ import annotations

from typing import Any


def _append_unique_metadata_list(session: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    values = list(session.setdefault("metadata", {}).get(key) or [])
    if value not in values:
        values.append(value)
    session.setdefault("metadata", {})[key] = values


def _apply_workspace_snapshot_metadata(session: dict[str, Any], snapshot: dict[str, Any]) -> None:
    metadata = session.setdefault("metadata", {})
    for key in ("workspaceId", "workspaceName", "workspaceRoot", "gitRoot", "gitBranch", "prNumber", "prUrl"):
        metadata[key] = snapshot.get(key)
    metadata["workspaceMode"] = snapshot.get("mode")
