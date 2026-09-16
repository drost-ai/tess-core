from __future__ import annotations

import asyncio
import os
import re
import stat
from typing import Any

from ._knob_reader import context_knob as _memory_knob
from .coerce import as_string_array
from ..payloads.bounded_io import read_file_prefix_bounded as _read_file_prefix_bounded
from ..payloads.utf8_bounds import slice_utf8_prefix as _slice_utf8_prefix
from ..payloads.utf8_bounds import utf8_byte_length as _utf8_byte_length
from ..storage import get_project_memory_path, get_project_root
from .durable_memory_compat import (
    base_memory_score,
    dedupe_memory_candidates,
    derive_keywords,
    derive_tags_from_text,
    durable_memory_id,
    estimate_memory_bytes,
    extract_durable_memory_candidates,
    normalize_memory_text,
    render_retrieved_memory_line,
    score_memory_record,
    truncate_memory_text,
)


MANUAL_MEMORY_RECORD_ID = "manual-project-memory"
_MAX_RETRIEVED_MEMORY_RECORDS = 1
_MAX_RETRIEVED_MEMORY_TOKEN_BUDGET = 1_600
_MAX_MANUAL_MEMORY_ACQUISITION_BYTES = 4_096
_MAX_MANUAL_MEMORY_PROMPT_TEXT_BYTES = 480
_DERIVED_RECORD_TIMESTAMP = "1970-01-01T00:00:00.000Z"
_MEMORY_PROMPT_PREFIX = [
    "Explicit manual project memory is available below.",
    "Treat it as workspace context, not as a summary of this session.",
    "Prefer the current user request, the current-session checkpoint, and recent raw turns if they conflict.",
    "",
    '<project_memory source="manual" scope="workspace">',
]
_MEMORY_PROMPT_SUFFIX = "</project_memory>"


async def read_manual_project_memory(cwd: str) -> dict[str, Any]:
    """Project the explicit workspace memory file without mutating durable state."""

    path = get_project_memory_path(cwd)
    try:
        await asyncio.to_thread(_assert_project_memory_parents_safe, path)
        manual_read = await _read_file_prefix_bounded(
            filePath=path,
            maxBytes=_MAX_MANUAL_MEMORY_ACQUISITION_BYTES,
            rejectSymlink=True,
        )
    except FileNotFoundError:
        return _empty_manual_projection(path)
    except (OSError, ValueError):
        return {**_empty_manual_projection(path), "unavailableReason": "unsafe_or_unreadable"}

    if manual_read.get("changed"):
        return {
            "path": path,
            "sourceBytes": manual_read["originalBytes"],
            "acquiredBytes": _utf8_byte_length(manual_read["text"]),
            "truncated": bool(manual_read["truncated"]),
            "changedDuringRead": True,
        }

    text = _project_manual_memory_text(
        manual_read["text"],
        int(manual_read["originalBytes"]),
        bool(manual_read["truncated"]),
    )
    projection: dict[str, Any] = {
        "path": path,
        "sourceBytes": manual_read["originalBytes"],
        "acquiredBytes": _utf8_byte_length(manual_read["text"]),
        "truncated": bool(manual_read["truncated"])
        or _utf8_byte_length(manual_read["text"]) > _MAX_MANUAL_MEMORY_PROMPT_TEXT_BYTES,
        "changedDuringRead": False,
    }
    if text:
        projection["record"] = {
            "id": MANUAL_MEMORY_RECORD_ID,
            "workspaceRoot": get_project_root(cwd),
            "createdAt": _DERIVED_RECORD_TIMESTAMP,
            "updatedAt": _DERIVED_RECORD_TIMESTAMP,
            "kind": "user_preference",
            "source": "manual",
            "text": text,
            "sourceDetail": "Explicit project memory (.tess/project/memory.md)",
            "confidence": "high",
        }
    return projection


async def synchronize_manual_project_memory(cwd: str) -> list[dict[str, Any]]:
    """Compatibility API: return a read-only projection of explicit memory.md."""

    projection = await read_manual_project_memory(cwd)
    record = projection.get("record")
    return [record] if isinstance(record, dict) else []


async def update_durable_memory_from_working_context(
    *positional: Any, **kwargs: Any
) -> list[dict[str, Any]]:
    """Compatibility API that no longer promotes session checkpoints.

    Compaction working context is session-scoped. It is intentionally ignored;
    only the explicit workspace memory file may be projected across sessions.
    """

    if (
        positional
        and isinstance(positional[0], dict)
        and ("session" in positional[0] or "workingContext" in positional[0])
        and len(positional) == 1
    ):
        args = {**positional[0], **kwargs}
        session = args.get("session") or {}
        options = args.get("options") or {}
        enabled = args.get("enabled", options.get("enabled"))
        cwd = args.get("cwd") or session.get("cwd")
    else:
        session = positional[0] if positional else kwargs.get("session") or {}
        options = (
            positional[2]
            if len(positional) > 2 and isinstance(positional[2], dict)
            else kwargs.get("options") or {}
        )
        enabled = kwargs.get("enabled", options.get("enabled"))
        cwd = kwargs.get("cwd") or (session or {}).get("cwd")

    if enabled is False or not cwd:
        return []
    return await synchronize_manual_project_memory(str(cwd))


async def retrieve_relevant_durable_memory(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Retrieve only explicit workspace memory; legacy automatic records are inert."""

    args = {**(args or {}), **kwargs}
    session = args.get("session") or {}
    max_records = min(
        _MAX_RETRIEVED_MEMORY_RECORDS,
        _normalize_non_negative_integer(
            args.get("maxRecords", _MAX_RETRIEVED_MEMORY_RECORDS)
        ),
    )
    max_token_budget = _normalize_non_negative_integer(
        args.get("maxTokenBudget", _MAX_RETRIEVED_MEMORY_TOKEN_BUDGET)
    )
    empty = {
        "records": [],
        "tokenEstimate": 0,
        "byteEstimate": 0,
        "maxPromptBytes": max_token_budget,
        "truncatedRecordIds": [],
    }
    if (
        args.get("enabled") is False
        or max_records == 0
        or max_token_budget == 0
        or not session.get("cwd")
    ):
        return empty

    projection = await read_manual_project_memory(str(session["cwd"]))
    record = projection.get("record")
    if not isinstance(record, dict):
        return empty

    separator_bytes = 1
    available_bytes = max_token_budget - _prompt_fixed_bytes() - separator_bytes
    if available_bytes <= 0:
        return empty
    bounded = _project_retrieved_memory_record(record, available_bytes)
    if not bounded:
        return empty

    rendered = render_retrieved_memory_line(bounded["record"])
    token_estimate = estimate_memory_bytes(rendered)
    byte_estimate = _prompt_fixed_bytes() + separator_bytes + _utf8_byte_length(rendered)
    if token_estimate > max_token_budget or byte_estimate > max_token_budget:
        return empty

    return {
        "records": [bounded["record"]],
        "tokenEstimate": token_estimate,
        "byteEstimate": byte_estimate,
        "maxPromptBytes": max_token_budget,
        "truncatedRecordIds": (
            [record["id"]]
            if projection.get("truncated") or bounded["truncated"]
            else []
        ),
    }


def render_retrieved_memory_prompt(memory: dict[str, Any]) -> str | None:
    max_bytes = int(
        memory.get("maxPromptBytes")
        if memory.get("maxPromptBytes") is not None
        else _MAX_RETRIEVED_MEMORY_TOKEN_BUDGET
    )
    manual = next(
        (
            record
            for record in (memory.get("records") or [])
            if record.get("id") == MANUAL_MEMORY_RECORD_ID
            and record.get("source") == "manual"
        ),
        None,
    )
    if not manual:
        return None
    rendered = "\n".join(
        [*_MEMORY_PROMPT_PREFIX, render_retrieved_memory_line(manual), _MEMORY_PROMPT_SUFFIX]
    )
    return rendered if _utf8_byte_length(rendered) <= max_bytes else None


# The helpers below remain exported for compatibility with the 0.5 Python
# surface. They no longer participate in admission or persistence.
def _project_retrieved_memory_record(
    record: dict[str, Any], max_line_bytes: int
) -> dict[str, Any] | None:
    rendered = render_retrieved_memory_line(record)
    if _utf8_byte_length(rendered) <= max_line_bytes:
        return {"record": record, "truncated": False}

    raw_bytes = _utf8_byte_length(str(record.get("text") or ""))
    source = _slice_utf8_prefix(
        str(record.get("sourceDetail") or record.get("id") or ""), 96
    )
    reserved = (
        f" … [truncated; source={source}; bytes={raw_bytes}; range=0-{raw_bytes}]"
    )
    overhead = _utf8_byte_length(
        render_retrieved_memory_line({**record, "text": ""})
    )
    available = max_line_bytes - overhead - _utf8_byte_length(reserved)
    if available <= 0:
        return None
    prefix = _slice_utf8_prefix(str(record.get("text") or ""), available).rstrip()
    provenance = (
        f" … [truncated; source={source}; bytes={raw_bytes}; "
        f"range=0-{_utf8_byte_length(prefix)}]"
    )
    projected = {**record, "text": f"{prefix}{provenance}"}
    if _utf8_byte_length(render_retrieved_memory_line(projected)) > max_line_bytes:
        return None
    return {"record": projected, "truncated": True}


def _empty_manual_projection(path: str) -> dict[str, Any]:
    return {
        "path": path,
        "sourceBytes": 0,
        "acquiredBytes": 0,
        "truncated": False,
        "changedDuringRead": False,
    }


def _project_manual_memory_text(
    acquired_text: str, source_bytes: int, source_truncated: bool
) -> str:
    normalized = re.sub(
        r"\s+",
        " ",
        re.sub(
            r"[\x00-\x1f\x7f]",
            " ",
            _slice_utf8_prefix(
                acquired_text, _MAX_MANUAL_MEMORY_PROMPT_TEXT_BYTES * 4
            ),
        ).strip(),
    )
    if not normalized:
        return ""
    needs_provenance = (
        source_truncated
        or _utf8_byte_length(normalized) > _MAX_MANUAL_MEMORY_PROMPT_TEXT_BYTES
    )
    suffix = (
        f" [project memory truncated from {source_bytes} bytes]"
        if needs_provenance
        else ""
    )
    available = max(
        0, _MAX_MANUAL_MEMORY_PROMPT_TEXT_BYTES - _utf8_byte_length(suffix)
    )
    return f"{_slice_utf8_prefix(normalized, available).rstrip()}{suffix}"


def _assert_project_memory_parents_safe(memory_path: str) -> None:
    project_state_dir = os.path.dirname(memory_path)
    tess_state_dir = os.path.dirname(project_state_dir)
    for directory in (tess_state_dir, project_state_dir):
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError(
                "Project memory state directories must be real directories, not symlinks."
            )


def _escape_prompt_markup(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _prompt_fixed_bytes() -> int:
    return _utf8_byte_length(
        "\n".join([*_MEMORY_PROMPT_PREFIX, _MEMORY_PROMPT_SUFFIX])
    )


def _normalize_non_negative_integer(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if numeric <= 0 or numeric == float("inf") or numeric != numeric:
        return 0
    return int(numeric)
