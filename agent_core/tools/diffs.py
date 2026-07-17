from __future__ import annotations

from typing import Any

from .file_change_diff import (
    build_create_diff_document,
    build_diff_context_indexes,
    build_line_diff_operations,
    build_unified_diff,
    build_write_diff_document,
)
from .validation import _without_none_values


def _file_change_payload(tool: str, file_path: str, previous: str | None, next_text: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    document = (
        build_create_diff_document(next_text)
        if previous is None
        else build_write_diff_document(previous, next_text)
    )
    payload = {
        "type": "file_change",
        "tool": tool,
        "action": "create" if previous is None else "update",
        "filePath": file_path,
        "bytes": len(next_text.encode("utf-8")),
        "note": (options or {}).get("note"),
        "fileBytes": len(next_text.encode("utf-8")),
        "addedLines": document["addedLines"],
        "removedLines": document["removedLines"],
        "diffStrategy": document["strategy"],
        "diff": document["diff"],
    }
    return _without_none_values(payload)


def _build_unified_diff(file_path: str, before_text: str, after_text: str) -> str:
    return build_unified_diff(file_path, before_text, after_text)


def _build_line_diff_operations(before_lines: list[str], after_lines: list[str]) -> list[dict[str, str]]:
    return build_line_diff_operations(before_lines, after_lines)


def _build_diff_context_indexes(operations: list[dict[str, str]], context_size: int) -> set[int]:
    return build_diff_context_indexes(operations, context_size)
