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
    extracted = _extract_compaction_memory_records(cwd, args.get("workingContext"), args.get("text"))
    if extracted:
        existing_keys = {(record.get("kind"), record.get("text")) for record in records if record.get("source") == "compaction"}
        new_records = [record for record in extracted if (record.get("kind"), record.get("text")) not in existing_keys]
        records = [*new_records, *records][:48]
        await saveProjectMemoryRecords(cwd, records)
    return records


def _extract_compaction_memory_records(cwd: str, workingContext: Any, fallbackText: Any = None) -> list[dict[str, Any]]:
    timestamp = now_iso()
    records: list[dict[str, Any]] = []

    def add(kind: str, text: Any, confidence: str = "medium") -> None:
        value = " ".join(str(text).split())
        if not value:
            return
        records.append(
            {
                "id": str(uuid.uuid4()),
                "workspaceRoot": cwd,
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "kind": kind,
                "source": "compaction",
                "text": value[:1200],
                "confidence": confidence,
            }
        )

    if isinstance(workingContext, dict):
        goals = workingContext.get("goals") or {}
        project = workingContext.get("project") or {}
        facts = project.get("facts") or {}
        current = workingContext.get("currentState") or {}
        for entry in goals.get("active") or []:
            add("task_context", entry, "high")
        for entry in workingContext.get("decisions") or []:
            add("decision", entry, "high")
        for entry in workingContext.get("discoveries") or []:
            add("repo_fact", entry, "medium")
        for entry in workingContext.get("criticalContext") or []:
            add("task_context", entry, "high")
        for entry in current.get("inProgress") or []:
            add("task_context", entry, "medium")
        for entry in current.get("blocked") or []:
            add("blocker", entry, "medium")
        for file_record in project.get("keyFiles") or []:
            if isinstance(file_record, dict) and file_record.get("path"):
                note = f": {file_record.get('note')}" if file_record.get("note") else ""
                add("repo_fact", f"{file_record.get('path')}{note}", "medium")
        if isinstance(project.get("description"), str):
            add("repo_fact", project["description"], "medium")
        for key in ("primaryLanguage", "ecosystem", "packageManager"):
            if isinstance(facts.get(key), str):
                add("repo_fact", f"{key}: {facts[key]}", "medium")
        for entry in facts.get("evidence") or []:
            add("repo_fact", entry, "medium")
    elif fallbackText:
        add("task_context", fallbackText, "medium")

    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for record in records:
        key = (record["kind"], record["text"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped[:16]


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
