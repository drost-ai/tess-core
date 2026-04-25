from __future__ import annotations

import os
import uuid
from typing import Any

from ._utils import now_iso
from .storage import getProjectMemoryPath, loadProjectMemoryRecords, saveProjectMemoryRecords


async def synchronizeManualProjectMemory(cwd: str) -> list[dict[str, Any]]:
    records = await loadProjectMemoryRecords(cwd)
    path = getProjectMemoryPath(cwd)
    if os.path.exists(path):
        text = open(path, encoding="utf-8").read().strip()
        if text:
            manual = {"id": "manual-project-memory", "workspaceRoot": cwd, "createdAt": now_iso(), "updatedAt": now_iso(), "kind": "repo_fact", "source": "manual", "text": text, "sourceDetail": path, "confidence": "high"}
            records = [record for record in records if record.get("id") != "manual-project-memory"]
            records.insert(0, manual)
            await saveProjectMemoryRecords(cwd, records[:48])
    return records


async def updateDurableMemoryFromWorkingContext(args: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    args = {**(args or {}), **kwargs}
    cwd = args.get("cwd") or args.get("session", {}).get("cwd")
    if not cwd:
        return []
    records = await loadProjectMemoryRecords(cwd)
    text = args.get("text") or args.get("workingContext")
    if text:
        records.insert(0, {"id": str(uuid.uuid4()), "workspaceRoot": cwd, "createdAt": now_iso(), "updatedAt": now_iso(), "kind": "task_context", "source": "compaction", "text": str(text), "confidence": "medium"})
        records = records[:48]
        await saveProjectMemoryRecords(cwd, records)
    return records


async def retrieveRelevantDurableMemory(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args.get("session", {})
    cwd = session.get("cwd")
    records = await loadProjectMemoryRecords(cwd) if cwd else []
    selected = records[:8]
    return {"records": selected, "tokenEstimate": sum((len(record.get("text", "")) + 3) // 4 for record in selected)}


def renderRetrievedMemoryPrompt(memory: dict[str, Any]) -> str | None:
    records = memory.get("records") or []
    if not records:
        return None
    lines = ["# Durable Memory"]
    for record in records:
        lines.append(f"- ({record.get('kind', 'memory')}) {record.get('text', '')}")
    return "\n".join(lines)
