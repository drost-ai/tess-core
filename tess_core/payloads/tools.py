from __future__ import annotations
from .._utils import drop_none_deep as _without_none_values

import os
import re
import uuid
from pathlib import Path
from typing import Any

import typing as _typing

from .._utils import now_iso
from ..storage import tool_artifact_data_path, tool_artifact_meta_path, write_json_file
from .utf8_bounds import slice_utf8_prefix as _slice_utf8_prefix

if _typing.TYPE_CHECKING:
    from ..contracts.tools import ToolArtifactRef


# One policy for every model. A tool's output comes back whole up to
# INLINE_OUTPUT_CHARS. Past that the whole output is saved to a file and the
# model gets a short notice with the file's path and the first PREVIEW_BYTES,
# which it reads further with Read. A file read returns up to
# READ_DEFAULT_LINES lines and is refused, not cut, past READ_MAX_TOKENS.
INLINE_OUTPUT_CHARS = 30_000
PREVIEW_BYTES = 2_000
READ_DEFAULT_LINES = 2_000
READ_MAX_TOKENS = 25_000
READ_BYTES_PER_TOKEN = 4

DEFAULT_GLOB_EXCLUDES = [
    ".git/**",
    ".svn/**",
    ".hg/**",
    "node_modules/**",
    "dist/**",
    "build/**",
    "coverage/**",
    ".next/**",
    ".nuxt/**",
    ".turbo/**",
    ".cache/**",
    ".yarn/**",
    ".venv/**",
    "venv/**",
    "env/**",
    "__pycache__/**",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
    ".tox/**",
    ".gradle/**",
    "tmp/**",
    "temp/**",
    "vendor/**",
    "target/**",
    ".DS_Store",
]
DEFAULT_IGNORED_DIRECTORIES = {
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    ".yarn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".gradle",
    "tmp",
    "temp",
    "vendor",
    "target",
}


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _count_lines(value: str) -> int:
    return 0 if value == "" else len(split_tool_text_lines(value))


def split_tool_text_lines(value: str) -> list[str]:
    return re.split(r"\r?\n", value)


def _format_size(byte_count: int) -> str:
    if byte_count < 1024:
        return f"{byte_count}B"
    if byte_count < 1024 * 1024:
        return f"{byte_count / 1024:.1f}KB"
    return f"{byte_count / (1024 * 1024):.1f}MB"


def over_inline_limit(text: str) -> bool:
    return len(text) > inline_output_chars()


def inline_output_chars() -> int:
    from ..config._knobs import knob_value

    return int(knob_value("payloads.inline_output_chars"))


def build_persisted_output(text: str, original_bytes: int, file_path: str | None) -> str:
    """What the model sees in place of an output that was too large to return whole."""
    from ..config._knobs import knob_value

    preview_bytes = int(knob_value("payloads.preview_bytes"))
    preview = _slice_utf8_prefix(text, preview_bytes)
    where = (
        f"Full output saved to: {file_path}"
        if file_path
        else "The full output could not be saved; only this preview is available."
    )
    more = "\n..." if _byte_len(preview) < _byte_len(text) or original_bytes > _byte_len(text) else ""
    return (
        f"<persisted-output>\nOutput too large ({_format_size(original_bytes)}). {where}\n\n"
        f"Preview (first {_format_size(preview_bytes)}):\n{preview}{more}\n</persisted-output>"
    )


async def _persist_artifact(args: dict[str, Any]) -> ToolArtifactRef:
    artifact_id = str(uuid.uuid4())
    content = args["content"]
    extension = ".json" if args.get("contentType") == "json" else ".txt"
    file_path = tool_artifact_data_path(args["sessionId"], artifact_id, extension)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    record = _without_none_values({
        "artifactId": artifact_id,
        "toolName": args["toolName"],
        "sessionId": args["sessionId"],
        "taskId": args.get("taskId"),
        "family": args["family"],
        "filePath": file_path,
        "bytes": _byte_len(content),
        "lines": _count_lines(content),
        "itemCount": args.get("itemCount"),
        "contentType": args.get("contentType", "text"),
        "createdAt": now_iso(),
        "truncated": True if args.get("truncated") else None,
        "sourceBytes": args.get("sourceBytes"),
    })
    await write_json_file(tool_artifact_meta_path(args["sessionId"], artifact_id), record)
    return record


def get_tool_payload_policy(config: dict[str, Any] | None = None) -> dict[str, int]:
    """The limits tools shape their results to. They are the same for every model."""
    from ..config._knobs import knob_value

    inline = inline_output_chars()
    read_lines = int(knob_value("payloads.read_default_lines"))
    return {
        "readDefaultLines": read_lines,
        "readInlineLines": read_lines,
        "readInlineBytes": int(knob_value("payloads.read_max_tokens")) * READ_BYTES_PER_TOKEN,
        "readMaxTokens": int(knob_value("payloads.read_max_tokens")),
        "globInlineItems": 100,
        "globInlineBytes": inline,
        "grepInlineMatches": 250,
        "grepInlineBytes": inline,
        "textInlineBytes": inline,
        "artifactThresholdBytes": inline,
    }


def get_default_glob_excludes() -> list[str]:
    return list(DEFAULT_GLOB_EXCLUDES)


def apply_default_glob_excludes(args: list[str]) -> list[str]:
    for pattern in DEFAULT_GLOB_EXCLUDES:
        args.extend(["-g", f"!{pattern}"])
    return args


def filter_default_ignored_glob_matches(matches: list[str]) -> list[str]:
    out = []
    for candidate in matches:
        normalized = candidate.replace("\\", "/")
        if normalized == ".DS_Store" or normalized.endswith("/.DS_Store"):
            continue
        segments = [part for part in normalized.split("/") if part]
        if not any(part in DEFAULT_IGNORED_DIRECTORIES for part in segments):
            out.append(candidate)
    return out


async def shape_text_tool_result(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """A tool's text output as the model gets it: whole, or saved with a preview when it is too large."""
    args = {**(args or {}), **kwargs}
    text = args.get("text", "")
    text_bytes = _byte_len(text)
    text_lines = _count_lines(text)
    original_bytes = max(text_bytes, int(args.get("originalBytes") or text_bytes))
    original_lines = max(text_lines, int(args.get("originalLines") or text_lines))
    source_already_truncated = bool(
        args.get("sourceTruncated") or original_bytes > text_bytes or original_lines > text_lines
    )
    artifact = args.get("existingArtifact")
    artifact_unavailable = bool(args.get("artifactUnavailable"))
    if not source_already_truncated and not over_inline_limit(text):
        return {
            "text": text,
            "view": _without_none_values({
                "strategy": "inline_full",
                "truncated": False,
                "originalBytes": original_bytes,
                "originalLines": original_lines,
                "returnedBytes": original_bytes,
                "returnedLines": original_lines,
                "artifact": artifact,
                "artifactUnavailable": True if artifact_unavailable else None,
            }),
        }
    if not artifact and args.get("persistArtifact") is not False:
        try:
            artifact = await _persist_artifact(
                {
                    **args,
                    "content": text,
                    "contentType": args.get("contentType", "text"),
                    "truncated": args.get("artifactTruncated"),
                    "sourceBytes": args.get("artifactSourceBytes"),
                }
            )
        except Exception:
            artifact_unavailable = True
    shown = build_persisted_output(text, original_bytes, artifact.get("filePath") if artifact else None)
    return _without_none_values({
        "text": shown,
        "view": {
            "strategy": "persisted_preview",
            "truncated": True,
            "originalBytes": original_bytes,
            "originalLines": original_lines,
            "returnedBytes": _byte_len(shown),
            "returnedLines": _count_lines(shown),
            "artifact": artifact,
            "artifactUnavailable": True if artifact_unavailable else None,
        },
    })
