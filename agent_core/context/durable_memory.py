from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from .._utils import now_iso
from ..storage import getProjectMemoryPath, getProjectRoot, loadProjectMemoryRecords as _loadProjectMemoryRecords
from ..storage import mutateProjectMemoryRecords
from ..todo_policy import selectTodoState as _select_todo_state


MANUAL_MEMORY_RECORD_ID = "manual-project-memory"
MAX_DURABLE_MEMORY_RECORDS = 48
MAX_MEMORY_TEXT_LENGTH = 480
MAX_RETRIEVED_MEMORY_RECORDS = 8
MAX_RETRIEVED_MEMORY_TOKEN_BUDGET = 1_600


async def synchronizeManualProjectMemory(cwd: str) -> list[dict[str, Any]]:
    path = getProjectMemoryPath(cwd)
    try:
        with open(path, encoding="utf-8") as handle:
            manual_text = handle.read().strip()
    except FileNotFoundError:
        manual_text = ""

    if not manual_text:
        current = await _loadProjectMemoryRecords(cwd)
        if not any(record.get("id") == MANUAL_MEMORY_RECORD_ID for record in current):
            return current

    def sync(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        without_manual = [record for record in records if record.get("id") != MANUAL_MEMORY_RECORD_ID]
        if not manual_text:
            return without_manual
        now = now_iso()
        previous = next((record for record in records if record.get("id") == MANUAL_MEMORY_RECORD_ID), None)
        manual = {
            "id": MANUAL_MEMORY_RECORD_ID,
            "workspaceRoot": getProjectRoot(cwd),
            "createdAt": (previous or {}).get("createdAt") or now,
            "updatedAt": now,
            "kind": "user_preference",
            "source": "manual",
            "text": manual_text,
            "sourceDetail": "Project memory notes",
            "confidence": "high",
            "tags": deriveTagsFromText(manual_text),
        }
        return [manual, *without_manual][:MAX_DURABLE_MEMORY_RECORDS]

    return await mutateProjectMemoryRecords(cwd, sync)


async def updateDurableMemoryFromWorkingContext(*positional: Any, **kwargs: Any) -> list[dict[str, Any]]:
    if positional and isinstance(positional[0], dict) and ("session" in positional[0] or "workingContext" in positional[0]) and len(positional) == 1:
        args = {**positional[0], **kwargs}
        session = args.get("session") or {}
        working_context = args.get("workingContext")
        cwd = args.get("cwd") or session.get("cwd")
    else:
        session = positional[0] if positional else kwargs.get("session") or {}
        working_context = positional[1] if len(positional) > 1 else kwargs.get("workingContext")
        cwd = kwargs.get("cwd") or (session or {}).get("cwd")
    if not cwd or not isinstance(working_context, dict):
        return []

    await synchronizeManualProjectMemory(cwd)
    now = now_iso()
    workspace_root = getProjectRoot(cwd)
    candidates = extractDurableMemoryCandidates(session or {}, working_context)

    def update(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        next_records = {record.get("id"): record for record in records if record.get("id")}
        for candidate in candidates:
            record_id = durableMemoryId(candidate["kind"], candidate["text"])
            previous = next_records.get(record_id) or {}
            record = {
                "id": record_id,
                "workspaceRoot": workspace_root,
                "createdAt": previous.get("createdAt") or now,
                "updatedAt": now,
                "kind": candidate["kind"],
                "source": "compaction",
                "text": candidate["text"],
                "sourceDetail": candidate.get("sourceDetail"),
                "confidence": candidate["confidence"],
            }
            if (session or {}).get("id") is not None:
                record["sessionId"] = (session or {}).get("id")
            if candidate.get("filePaths") is not None:
                record["filePaths"] = candidate.get("filePaths")
            if candidate.get("tags") is not None:
                record["tags"] = candidate.get("tags")
            if previous.get("lastUsedAt") is not None:
                record["lastUsedAt"] = previous.get("lastUsedAt")
            next_records[record_id] = record
        persisted = list(next_records.values())
        persisted.sort(key=lambda record: record.get("updatedAt", ""), reverse=True)
        persisted.sort(key=lambda record: 0 if record.get("id") == MANUAL_MEMORY_RECORD_ID else 1)
        return persisted[:MAX_DURABLE_MEMORY_RECORDS]

    return await mutateProjectMemoryRecords(cwd, update)


async def retrieveRelevantDurableMemory(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args.get("session") or {}
    cwd = session.get("cwd")
    if not cwd:
        return {"records": [], "tokenEstimate": 0}
    max_records = int(args.get("maxRecords") or MAX_RETRIEVED_MEMORY_RECORDS)
    max_token_budget = int(args.get("maxTokenBudget") or MAX_RETRIEVED_MEMORY_TOKEN_BUDGET)
    records = await synchronizeManualProjectMemory(cwd)
    selected_todos = _select_todo_state(session.get("todos", []), {"maxOpen": 20, "maxCompleted": 0})
    keywords = deriveKeywords(
        [
            args.get("prompt") or "",
            *[
                " ".join(str(part) for part in (todo.get("content"), todo.get("activeForm")) if part)
                for todo in selected_todos["open"]
            ],
        ]
    )
    ranked = [
        {"record": record, "score": scoreMemoryRecord(record, keywords)}
        for record in records
    ]
    ranked = [entry for entry in ranked if entry["score"] > 0]
    ranked.sort(key=lambda entry: (entry["score"], entry["record"].get("updatedAt", "")), reverse=True)

    selected: list[dict[str, Any]] = []
    token_estimate = 0
    for entry in ranked:
        if len(selected) >= max_records:
            break
        next_token_estimate = estimateTextTokens(renderRetrievedMemoryLine(entry["record"]))
        if selected and token_estimate + next_token_estimate > max_token_budget:
            continue
        selected.append(entry["record"])
        token_estimate += next_token_estimate

    if selected:
        selected_ids = {record["id"] for record in selected}
        now = now_iso()
        await mutateProjectMemoryRecords(
            cwd,
            lambda current: [
                {**record, "lastUsedAt": now} if record.get("id") in selected_ids else record
                for record in current
            ],
        )

    return {"records": selected, "tokenEstimate": token_estimate}


def renderRetrievedMemoryPrompt(memory: dict[str, Any]) -> str | None:
    records = memory.get("records") or []
    if not records:
        return None
    return "\n".join(
        [
            "Relevant durable memory for this workspace is available below.",
            "Use it when it helps continuation, but prefer recent raw turns and current working context if they conflict.",
            "",
            "<retrieved_memory>",
            *[renderRetrievedMemoryLine(record) for record in records],
            "</retrieved_memory>",
        ]
    )


def extractDurableMemoryCandidates(session: dict[str, Any], workingContext: dict[str, Any]) -> list[dict[str, Any]]:
    project = workingContext.get("project") or {}
    goals = workingContext.get("goals") or {}
    file_paths = [record.get("path") for record in (project.get("keyFiles") or [])[:6] if isinstance(record, dict) and record.get("path")]
    tags = deriveTagsFromText(
        " ".join(
            [
                project.get("description") or "",
                *asStringArray(goals.get("active")),
                *asStringArray(workingContext.get("decisions")),
                *asStringArray(workingContext.get("discoveries")),
                *asStringArray(workingContext.get("criticalContext")),
            ]
        )
    )
    facts = project.get("facts") or {}
    candidates: list[dict[str, Any]] = []
    if isinstance(project.get("description"), str):
        candidates.append({"kind": "repo_fact", "text": project["description"], "sourceDetail": "Project description", "confidence": "medium"})
    for entry in asStringArray(facts.get("evidence")):
        candidates.append({"kind": "repo_fact", "text": entry, "sourceDetail": "Project evidence", "filePaths": file_paths, "confidence": "medium"})
    for entry in asStringArray(goals.get("constraints")):
        candidates.append({"kind": "constraint", "text": entry, "sourceDetail": "Task constraint", "confidence": "high"})
    for entry in asStringArray(goals.get("preferences")):
        candidates.append({"kind": "user_preference", "text": entry, "sourceDetail": "User preference", "confidence": "high"})
    for entry in asStringArray(workingContext.get("decisions")):
        candidates.append({"kind": "workflow", "text": entry, "sourceDetail": "Key decision", "filePaths": file_paths, "tags": tags, "confidence": "medium"})
    for entry in asStringArray(workingContext.get("discoveries")):
        candidates.append({"kind": "task_context", "text": entry, "sourceDetail": "Discovery", "filePaths": file_paths, "tags": tags, "confidence": "medium"})
    for entry in asStringArray(workingContext.get("criticalContext")):
        candidates.append({"kind": "environment", "text": entry, "sourceDetail": "Critical continuation context", "filePaths": file_paths, "tags": tags, "confidence": "medium"})
    return dedupeMemoryCandidates([{**entry, "text": truncateMemoryText(entry["text"])} for entry in candidates if truncateMemoryText(entry.get("text", ""))])


def dedupeMemoryCandidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for candidate in candidates:
        key = f"{candidate.get('kind')}:{normalizeMemoryText(candidate.get('text', ''))}"
        if not candidate.get("text") or key in seen:
            continue
        seen.add(key)
        output.append(candidate)
    return output


def renderRetrievedMemoryLine(record: dict[str, Any]) -> str:
    meta = " | ".join([str(record.get("kind")), str(record.get("source")), *[str(path) for path in (record.get("filePaths") or [])[:3]]])
    return f"- [{meta}] {record.get('text', '')}"


def scoreMemoryRecord(record: dict[str, Any], keywords: list[str]) -> int:
    score = baseMemoryScore(record.get("kind")) + (5 if record.get("source") == "manual" else 0)
    if not keywords:
        return score
    haystack = " ".join(
        [
            record.get("text") or "",
            *asStringArray(record.get("tags")),
            *asStringArray(record.get("filePaths")),
            record.get("sourceDetail") or "",
        ]
    ).lower()
    matches = sum(1 for keyword in keywords if keyword in haystack)
    score += matches * 3
    if record.get("lastUsedAt"):
        score += 1
    return score


def baseMemoryScore(kind: Any) -> int:
    if kind in {"user_preference", "constraint"}:
        return 8
    if kind == "environment":
        return 6
    if kind in {"repo_fact", "workflow"}:
        return 5
    if kind == "task_context":
        return 4
    return 0


def durableMemoryId(kind: str, text: str) -> str:
    return hashlib.sha1(f"{kind}:{normalizeMemoryText(text)}".encode("utf-8")).hexdigest()


def normalizeMemoryText(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def truncateMemoryText(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text).strip())
    if len(normalized) <= MAX_MEMORY_TEXT_LENGTH:
        return normalized
    return normalized[: MAX_MEMORY_TEXT_LENGTH - 1].rstrip() + "…"


def deriveKeywords(values: list[str]) -> list[str]:
    seen: set[str] = set()
    words: list[str] = []
    for value in values:
        for word in re.findall(r"[a-z0-9_./-]{4,}", str(value).lower()):
            if word in seen:
                continue
            seen.add(word)
            words.append(word)
    return words


def deriveTagsFromText(value: str) -> list[str]:
    return deriveKeywords([value])[:12]


def estimateTextTokens(value: str) -> int:
    return (len(value) + 3) // 4


def asStringArray(value: Any) -> list[str]:
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []
