from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

import typing as _typing

from .._utils import now_iso
from ..storage import toolArtifactDataPath, toolArtifactMetaPath, writeJsonFile
from .utf8_bounds import sliceUtf8Suffix as _slice_utf8_suffix

if _typing.TYPE_CHECKING:
    from ..contracts.tools import ToolArtifactRef


TOOL_PAYLOAD_POLICIES = {
    "small": {
        "readDefaultLines": 140,
        "readInlineLines": 180,
        "readInlineBytes": 8000,
        "globInlineItems": 80,
        "globInlineBytes": 6000,
        "grepInlineMatches": 60,
        "grepInlineBytes": 8000,
        "textInlineLines": 140,
        "textInlineBytes": 8000,
        "textHeadLines": 30,
        "textTailLines": 60,
        "artifactThresholdBytes": 6000,
        "artifactThresholdLines": 180,
    },
    "medium": {
        "readDefaultLines": 220,
        "readInlineLines": 260,
        "readInlineBytes": 12000,
        "globInlineItems": 100,
        "globInlineBytes": 8000,
        "grepInlineMatches": 100,
        "grepInlineBytes": 12000,
        "textInlineLines": 220,
        "textInlineBytes": 12000,
        "textHeadLines": 40,
        "textTailLines": 90,
        "artifactThresholdBytes": 10000,
        "artifactThresholdLines": 260,
    },
    "large": {
        "readDefaultLines": 320,
        "readInlineLines": 420,
        "readInlineBytes": 18000,
        "globInlineItems": 120,
        "globInlineBytes": 10000,
        "grepInlineMatches": 160,
        "grepInlineBytes": 18000,
        "textInlineLines": 320,
        "textInlineBytes": 18000,
        "textHeadLines": 60,
        "textTailLines": 120,
        "artifactThresholdBytes": 14000,
        "artifactThresholdLines": 420,
    },
}

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
    return 0 if value == "" else len(splitToolTextLines(value))


def splitToolTextLines(value: str) -> list[str]:
    return re.split(r"\r?\n", value)


def _without_none_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none_values(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none_values(item) for item in value]
    return value


def _fit_lines_within_bytes(lines: list[str], max_bytes: int) -> list[str]:
    selected: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not selected else f"{current}\n{line}"
        if _byte_len(candidate) > max_bytes:
            break
        selected.append(line)
        current = candidate
    return selected




def _head_tail_excerpt(text: str, head_lines: int, tail_lines: int, max_bytes: int) -> dict[str, Any]:
    lines = splitToolTextLines(text)
    head = _fit_lines_within_bytes(lines[:head_lines], max_bytes)
    tail = _fit_lines_within_bytes(lines[max(len(lines) - tail_lines, 0):], max_bytes)
    excerpt = "\n".join(head)
    omitted = max(len(lines) - len(head) - len(tail), 0)
    if tail and len(lines) > len(head):
        separator = f"\n...[omitted {omitted} lines]...\n" if omitted else "\n...\n"
        excerpt = f"{excerpt}{separator}{chr(10).join(tail)}".strip()
    if not excerpt:
        return {"text": _slice_utf8_suffix(text, max_bytes).strip(), "truncated": True, "strategy": "inline_truncated_tail"}
    if _byte_len(excerpt) > max_bytes:
        return {"text": _slice_utf8_suffix(excerpt, max_bytes).strip(), "truncated": True, "strategy": "inline_truncated_tail"}
    return {"text": excerpt, "truncated": excerpt != text, "strategy": "inline_full" if excerpt == text else "inline_head_tail"}


async def _persist_artifact(args: dict[str, Any]) -> ToolArtifactRef:
    artifact_id = str(uuid.uuid4())
    content = args["content"]
    extension = ".json" if args.get("contentType") == "json" else ".txt"
    file_path = toolArtifactDataPath(args["sessionId"], artifact_id, extension)
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
    await writeJsonFile(toolArtifactMetaPath(args["sessionId"], artifact_id), record)
    return record


def deriveToolPayloadClass(config: dict[str, Any]) -> str:
    model = str(config.get("model", "")).lower()
    if config.get("providerKind") == "anthropic":
        return "large"
    if config.get("providerKind") == "openai" and config.get("openaiBaseUrl"):
        if any(token in model for token in ("minimax", "glm", "qwen", "llama", "mistral", "gemma", "deepseek", "phi", "mixtral", "command-r", "yi", "small", "tiny", "nano")):
            return "small"
        return "medium" if config.get("openaiApiMode") == "responses" else "small"
    if "mini" in model or "spark" in model:
        return "medium"
    return "large"


def getToolPayloadPolicy(config: dict[str, Any]) -> dict[str, int]:
    return TOOL_PAYLOAD_POLICIES[deriveToolPayloadClass(config)]


def getDefaultGlobExcludes() -> list[str]:
    return list(DEFAULT_GLOB_EXCLUDES)


def applyDefaultGlobExcludes(args: list[str]) -> list[str]:
    for pattern in DEFAULT_GLOB_EXCLUDES:
        args.extend(["-g", f"!{pattern}"])
    return args


def filterDefaultIgnoredGlobMatches(matches: list[str]) -> list[str]:
    out = []
    for candidate in matches:
        normalized = candidate.replace("\\", "/")
        if normalized == ".DS_Store" or normalized.endswith("/.DS_Store"):
            continue
        segments = [part for part in normalized.split("/") if part]
        if not any(part in DEFAULT_IGNORED_DIRECTORIES for part in segments):
            out.append(candidate)
    return out


async def shapeTextToolResult(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    policy = getToolPayloadPolicy(args["config"])
    text = args.get("text", "")
    text_bytes = _byte_len(text)
    text_lines = _count_lines(text)
    original_bytes = max(text_bytes, int(args.get("originalBytes") or text_bytes))
    original_lines = max(text_lines, int(args.get("originalLines") or text_lines))
    source_already_truncated = bool(
        args.get("sourceTruncated") or original_bytes > text_bytes or original_lines > text_lines
    )
    needs_truncation = bool(
        source_already_truncated
        or text_bytes > policy["textInlineBytes"]
        or text_lines > policy["textInlineLines"]
    )
    if not needs_truncation:
        return {
            "text": text,
            "view": _without_none_values({
                "strategy": "inline_full",
                "truncated": False,
                "originalBytes": original_bytes,
                "originalLines": original_lines,
                "returnedBytes": original_bytes,
                "returnedLines": original_lines,
                "artifact": args.get("existingArtifact"),
                "artifactUnavailable": True if args.get("artifactUnavailable") else None,
            }),
        }
    strategy = args.get("strategy") or "head"
    if strategy == "tail":
        excerpt = _head_tail_excerpt(text, 0, policy["textTailLines"], policy["textInlineBytes"])
    elif strategy == "head_tail":
        excerpt = _head_tail_excerpt(text, policy["textHeadLines"], policy["textTailLines"], policy["textInlineBytes"])
    else:
        lines = _fit_lines_within_bytes(splitToolTextLines(text)[: policy["textInlineLines"]], policy["textInlineBytes"])
        next_text = "\n".join(lines).strip()
        excerpt = {"text": next_text, "truncated": next_text != text, "strategy": "inline_full" if next_text == text else "inline_truncated_head"}
    artifact = args.get("existingArtifact")
    artifact_unavailable = bool(args.get("artifactUnavailable"))
    artifact_needed = bool(
        not artifact
        and args.get("persistArtifact") is not False
        and (
            original_bytes > policy["artifactThresholdBytes"]
            or original_lines > policy["artifactThresholdLines"]
        )
    )
    if artifact_needed:
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
    return _without_none_values({
        "text": excerpt["text"],
        "view": {
            "strategy": (
                "inline_head_tail"
                if source_already_truncated and excerpt["strategy"] == "inline_full"
                else excerpt["strategy"]
            ),
            "truncated": source_already_truncated or excerpt["truncated"],
            "originalBytes": original_bytes,
            "originalLines": original_lines,
            "returnedBytes": _byte_len(excerpt["text"]),
            "returnedLines": _count_lines(excerpt["text"]),
            "artifact": artifact,
            "artifactUnavailable": True if artifact_unavailable else None,
        },
    })








