from __future__ import annotations

import hashlib
import re
from typing import Any

from ._knob_reader import context_knob as _memory_knob
from .coerce import as_string_array
from ..payloads.utf8_bounds import slice_utf8_prefix as _slice_utf8_prefix
from ..payloads.utf8_bounds import utf8_byte_length as _utf8_byte_length
from ..storage import get_project_root


def extract_durable_memory_candidates(
    session: dict[str, Any], workingContext: dict[str, Any]
) -> list[dict[str, Any]]:
    project = workingContext.get("project") or {}
    goals = workingContext.get("goals") or {}
    file_paths = [
        record.get("path")
        for record in (project.get("keyFiles") or [])[:6]
        if isinstance(record, dict) and record.get("path")
    ]
    tags = derive_tags_from_text(
        " ".join(
            [
                project.get("description") or "",
                *as_string_array(goals.get("active")),
                *as_string_array(workingContext.get("decisions")),
                *as_string_array(workingContext.get("discoveries")),
                *as_string_array(workingContext.get("criticalContext")),
            ]
        )
    )
    facts = project.get("facts") or {}
    candidates: list[dict[str, Any]] = []
    if isinstance(project.get("description"), str):
        candidates.append(
            {
                "kind": "repo_fact",
                "text": project["description"],
                "sourceDetail": "Project description",
                "confidence": "medium",
            }
        )
    for entry in as_string_array(facts.get("evidence")):
        candidates.append(
            {
                "kind": "repo_fact",
                "text": entry,
                "sourceDetail": "Project evidence",
                "filePaths": file_paths,
                "confidence": "medium",
            }
        )
    for entry in as_string_array(goals.get("constraints")):
        candidates.append(
            {
                "kind": "constraint",
                "text": entry,
                "sourceDetail": "Task constraint",
                "confidence": "high",
            }
        )
    for entry in as_string_array(goals.get("preferences")):
        candidates.append(
            {
                "kind": "user_preference",
                "text": entry,
                "sourceDetail": "User preference",
                "confidence": "high",
            }
        )
    for entry in as_string_array(workingContext.get("decisions")):
        candidates.append(
            {
                "kind": "workflow",
                "text": entry,
                "sourceDetail": "Key decision",
                "filePaths": file_paths,
                "tags": tags,
                "confidence": "medium",
            }
        )
    for entry in as_string_array(workingContext.get("discoveries")):
        candidates.append(
            {
                "kind": "task_context",
                "text": entry,
                "sourceDetail": "Discovery",
                "filePaths": file_paths,
                "tags": tags,
                "confidence": "medium",
            }
        )
    for entry in as_string_array(workingContext.get("criticalContext")):
        candidates.append(
            {
                "kind": "environment",
                "text": entry,
                "sourceDetail": "Critical continuation context",
                "filePaths": file_paths,
                "tags": tags,
                "confidence": "high",
            }
        )
    return dedupe_memory_candidates(candidates)


def dedupe_memory_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for candidate in candidates:
        text = truncate_memory_text(candidate.get("text") or "")
        key = f"{candidate.get('kind')}:{normalize_memory_text(text)}"
        if not text or key in seen:
            continue
        seen.add(key)
        deduped.append({**candidate, "text": text})
    return deduped


def _escape_prompt_markup(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_retrieved_memory_line(record: dict[str, Any]) -> str:
    text = _escape_prompt_markup(str(record.get("text") or ""))
    return f"- [manual | project] {text}"


def score_memory_record(record: dict[str, Any], keywords: list[str]) -> int:
    score = base_memory_score(record.get("kind")) + (
        5 if record.get("source") == "manual" else 0
    )
    if not keywords:
        return score
    haystack = " ".join(
        [
            _slice_utf8_prefix(record.get("text") or "", 2_000),
            *[
                _slice_utf8_prefix(value, 120)
                for value in as_string_array(record.get("tags"))[:24]
            ],
            *[
                _slice_utf8_prefix(value, 240)
                for value in as_string_array(record.get("filePaths"))[:12]
            ],
            _slice_utf8_prefix(record.get("sourceDetail") or "", 240),
        ]
    ).lower()
    score += sum(1 for keyword in keywords if keyword in haystack) * 3
    if record.get("lastUsedAt"):
        score += 1
    return score


def base_memory_score(kind: Any) -> int:
    if kind in {"user_preference", "constraint"}:
        return 8
    if kind == "environment":
        return 6
    if kind in {"repo_fact", "workflow"}:
        return 5
    if kind == "task_context":
        return 4
    return 0


def durable_memory_id(kind: str, text: str) -> str:
    return hashlib.sha1(f"{kind}:{normalize_memory_text(text)}".encode("utf-8")).hexdigest()


def normalize_memory_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def truncate_memory_text(text: str, provenance: str = "") -> str:
    source = str(text)
    source_bytes = _utf8_byte_length(source)
    normalized = re.sub(
        r"\s+",
        " ",
        _slice_utf8_prefix(source, _memory_knob("memory.max_text_chars") * 4).strip(),
    )
    suffix = provenance or (
        f" [truncated from {source_bytes} bytes]"
        if source_bytes > _memory_knob("memory.max_text_chars")
        else ""
    )
    available = max(
        0, _memory_knob("memory.max_text_chars") - _utf8_byte_length(suffix)
    )
    return f"{_slice_utf8_prefix(normalized, available).rstrip()}{suffix}"


def derive_keywords(values: list[str]) -> list[str]:
    seen: set[str] = set()
    words: list[str] = []
    for value in values:
        for word in re.findall(
            r"[a-z0-9_./-]{4,}",
            _slice_utf8_prefix(str(value), 8_000).lower(),
        ):
            if word in seen:
                continue
            seen.add(word)
            words.append(word)
    return words


def derive_tags_from_text(value: str) -> list[str]:
    return derive_keywords([value])[:12]


def estimate_memory_bytes(value: str) -> int:
    return _utf8_byte_length(value)
