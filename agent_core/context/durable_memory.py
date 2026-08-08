from __future__ import annotations

from .coerce import asStringArray

import hashlib
import os
import re
from typing import Any

from .._utils import now_iso
from ..payloads.bounded_io import readFilePrefixBounded as _read_file_prefix_bounded
from ..storage import getProjectMemoryPath, getProjectRoot, loadProjectMemoryRecords as _loadProjectMemoryRecords
from ..storage import mutateProjectMemoryRecords
from ..payloads.todo_policy import selectTodoState as _select_todo_state
from ..payloads.utf8_bounds import sliceUtf8Prefix as _slice_utf8_prefix
from ..payloads.utf8_bounds import utf8ByteLength as _utf8_byte_length


MANUAL_MEMORY_RECORD_ID = "manual-project-memory"
from ._knob_reader import context_knob as _memory_knob
_MAX_MANUAL_MEMORY_ACQUISITION_BYTES = 4_096
_MEMORY_PROMPT_PREFIX = [
    "Relevant durable memory for this workspace is available below.",
    "Use it when it helps continuation, but prefer recent raw turns and current working context if they conflict.",
    "",
    "<retrieved_memory>",
]
_MEMORY_PROMPT_SUFFIX = "</retrieved_memory>"


async def synchronizeManualProjectMemory(cwd: str) -> list[dict[str, Any]]:
    path = getProjectMemoryPath(cwd)
    try:
        manual_read = await _read_file_prefix_bounded(
            filePath=path, maxBytes=_MAX_MANUAL_MEMORY_ACQUISITION_BYTES
        )
    except (OSError, ValueError):
        manual_read = None
    manual_text = (
        truncateMemoryText(
            manual_read["text"],
            (
                f" [manual memory projected from {manual_read['originalBytes']} bytes; "
                f"source range 0-{_MAX_MANUAL_MEMORY_ACQUISITION_BYTES}]"
                if manual_read["truncated"]
                else ""
            ),
        )
        if manual_read
        else ""
    )

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
        return [manual, *without_manual][:_memory_knob("memory.max_records")]

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
        return persisted[:_memory_knob("memory.max_records")]

    return await mutateProjectMemoryRecords(cwd, update)


async def retrieveRelevantDurableMemory(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    if args.get("enabled") is False:
        return {"records": [], "tokenEstimate": 0}
    session = args.get("session") or {}
    cwd = session.get("cwd")
    if not cwd:
        return {"records": [], "tokenEstimate": 0}
    max_records_value = args.get("maxRecords")
    max_records = max(
        0,
        int(
            _memory_knob("memory.retrieved_records")
            if max_records_value is None
            else max_records_value
        ),
    )
    max_token_budget_value = args.get("maxTokenBudget")
    max_token_budget = max(
        0,
        int(
            _memory_knob("memory.retrieved_token_budget")
            if max_token_budget_value is None
            else max_token_budget_value
        ),
    )
    max_prompt_bytes = max_token_budget
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
    truncated_record_ids: list[str] = []
    token_estimate = 0
    byte_estimate = _prompt_fixed_bytes()
    for entry in ranked:
        if len(selected) >= max_records:
            break
        separator_bytes = 1
        available_bytes = max_prompt_bytes - byte_estimate - separator_bytes
        if available_bytes <= 0:
            break
        projection = _project_retrieved_memory_record(entry["record"], available_bytes)
        if not projection:
            continue
        rendered = renderRetrievedMemoryLine(projection["record"])
        next_bytes = _utf8_byte_length(rendered) + separator_bytes
        next_token_estimate = estimateMemoryBytes(rendered)
        if (
            byte_estimate + next_bytes > max_prompt_bytes
            or token_estimate + next_token_estimate > max_token_budget
        ):
            continue
        selected.append(projection["record"])
        if projection["truncated"]:
            truncated_record_ids.append(entry["record"]["id"])
        token_estimate += next_token_estimate
        byte_estimate += next_bytes

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

    return {
        "records": selected,
        "tokenEstimate": token_estimate,
        "byteEstimate": byte_estimate,
        "maxPromptBytes": max_prompt_bytes,
        "truncatedRecordIds": truncated_record_ids,
    }


def renderRetrievedMemoryPrompt(memory: dict[str, Any]) -> str | None:
    records = memory.get("records") or []
    if not records:
        return None
    max_bytes = int(memory.get("maxPromptBytes") or _memory_knob("memory.retrieved_token_budget"))
    lines = list(_MEMORY_PROMPT_PREFIX)
    for record in records:
        rendered = renderRetrievedMemoryLine(record)
        candidate = "\n".join([*lines, rendered, _MEMORY_PROMPT_SUFFIX])
        if _utf8_byte_length(candidate) > max_bytes:
            break
        lines.append(rendered)
    if len(lines) == len(_MEMORY_PROMPT_PREFIX):
        return None
    rendered_prompt = "\n".join([*lines, _MEMORY_PROMPT_SUFFIX])
    return rendered_prompt if _utf8_byte_length(rendered_prompt) <= max_bytes else None


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
    bounded_candidates: list[dict[str, Any]] = []
    for entry in candidates:
        text = truncateMemoryText(entry.get("text", ""))
        if text:
            bounded_candidates.append({**entry, "text": text})
    return dedupeMemoryCandidates(bounded_candidates)


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
    meta = " | ".join(
        [
            str(record.get("kind")),
            str(record.get("source")),
            *[_slice_utf8_prefix(str(path), 120) for path in (record.get("filePaths") or [])[:3]],
        ]
    )
    return f"- [{_slice_utf8_prefix(meta, 420)}] {record.get('text', '')}"


def scoreMemoryRecord(record: dict[str, Any], keywords: list[str]) -> int:
    score = baseMemoryScore(record.get("kind")) + (5 if record.get("source") == "manual" else 0)
    if not keywords:
        return score
    haystack = " ".join(
        [
            _slice_utf8_prefix(record.get("text") or "", 2_000),
            *[_slice_utf8_prefix(value, 120) for value in asStringArray(record.get("tags"))[:24]],
            *[_slice_utf8_prefix(value, 240) for value in asStringArray(record.get("filePaths"))[:12]],
            _slice_utf8_prefix(record.get("sourceDetail") or "", 240),
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


def truncateMemoryText(text: str, provenance: str = "") -> str:
    source = str(text)
    source_bytes = _utf8_byte_length(source)
    normalized = re.sub(
        r"\s+", " ", _slice_utf8_prefix(source, _memory_knob("memory.max_text_chars") * 4).strip()
    )
    suffix = provenance or (
        f" [truncated from {source_bytes} bytes]"
        if source_bytes > _memory_knob("memory.max_text_chars")
        else ""
    )
    available = max(0, _memory_knob("memory.max_text_chars") - _utf8_byte_length(suffix))
    return f"{_slice_utf8_prefix(normalized, available).rstrip()}{suffix}"


def deriveKeywords(values: list[str]) -> list[str]:
    seen: set[str] = set()
    words: list[str] = []
    for value in values:
        for word in re.findall(
            r"[a-z0-9_./-]{4,}", _slice_utf8_prefix(str(value), 8_000).lower()
        ):
            if word in seen:
                continue
            seen.add(word)
            words.append(word)
    return words


def deriveTagsFromText(value: str) -> list[str]:
    return deriveKeywords([value])[:12]


def estimateMemoryBytes(value: str) -> int:
    return _utf8_byte_length(value)


def _project_retrieved_memory_record(
    record: dict[str, Any], max_line_bytes: int
) -> dict[str, Any] | None:
    bounded_paths = [
        _slice_utf8_prefix(str(value), 120)
        for value in (record.get("filePaths") or [])[:3]
    ]
    base = {**record, **({"filePaths": bounded_paths} if record.get("filePaths") else {})}
    raw_bytes = _utf8_byte_length(str(record.get("text") or ""))
    if _utf8_byte_length(renderRetrievedMemoryLine(base)) <= max_line_bytes:
        return {"record": base, "truncated": False}
    source = _slice_utf8_prefix(
        str(record.get("sourceDetail") or record.get("id") or ""), 96
    )
    reserved = f" … [truncated; source={source}; bytes={raw_bytes}; range=0-{raw_bytes}]"
    overhead = _utf8_byte_length(renderRetrievedMemoryLine({**base, "text": ""}))
    available = max_line_bytes - overhead - _utf8_byte_length(reserved)
    if available <= 0:
        return None
    prefix = _slice_utf8_prefix(str(record.get("text") or ""), available).rstrip()
    provenance = (
        f" … [truncated; source={source}; bytes={raw_bytes}; "
        f"range=0-{_utf8_byte_length(prefix)}]"
    )
    projected = {**base, "text": f"{prefix}{provenance}"}
    if _utf8_byte_length(renderRetrievedMemoryLine(projected)) > max_line_bytes:
        return None
    return {"record": projected, "truncated": True}


def _prompt_fixed_bytes() -> int:
    return _utf8_byte_length(
        "\n".join([*_MEMORY_PROMPT_PREFIX, _MEMORY_PROMPT_SUFFIX])
    )


