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
from .utf8_bounds import slice_utf8_suffix as _slice_utf8_suffix

if _typing.TYPE_CHECKING:
    from ..contracts.tools import ToolArtifactRef


# text* line caps are backstops against pathological many-tiny-line output;
# the byte budget is the intended binding constraint. Keep each line cap at
# roughly textInlineBytes / 20 (short-lined text like stripped HTML averages
# ~20 bytes/line), or short results get excerpted with budget left over and
# the model pays artifact round trips for content that fit inline.
TOOL_PAYLOAD_POLICIES = {
    "small": {
        "readDefaultLines": 140,
        "readInlineLines": 180,
        "readInlineBytes": 8000,
        "globInlineItems": 80,
        "globInlineBytes": 6000,
        "grepInlineMatches": 60,
        "grepInlineBytes": 8000,
        "textInlineLines": 400,
        "textInlineBytes": 8000,
        "textHeadLines": 100,
        "textTailLines": 200,
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
        "textInlineLines": 600,
        "textInlineBytes": 12000,
        "textHeadLines": 150,
        "textTailLines": 300,
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
        "textInlineLines": 900,
        "textInlineBytes": 18000,
        "textHeadLines": 200,
        "textTailLines": 400,
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
    return 0 if value == "" else len(split_tool_text_lines(value))


def split_tool_text_lines(value: str) -> list[str]:
    return re.split(r"\r?\n", value)


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
    lines = split_tool_text_lines(text)
    # Budget the halves jointly (head up to a third, tail the rest) so the
    # combined excerpt fits max_bytes at any line cap; a lone half keeps the
    # whole budget. The tail window starts after the head so overlapping
    # windows never duplicate lines.
    head_budget = max_bytes // 3 if head_lines and tail_lines else max_bytes
    head = _fit_lines_within_bytes(lines[:head_lines], head_budget)
    tail_budget = max(max_bytes - _byte_len("\n".join(head)) - (64 if head else 0), 0)
    tail_start = max(len(lines) - tail_lines, len(head))
    tail = _fit_lines_within_bytes(lines[tail_start:], tail_budget)
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


def derive_tool_payload_class(config: dict[str, Any]) -> str:
    profile = config.get("providerProfile") if isinstance(config.get("providerProfile"), dict) else {}
    declared = profile.get("payloadClass")
    if declared in TOOL_PAYLOAD_POLICIES:
        # An explicit providers.yaml payloadClass beats the model-name
        # heuristic below — names like "deepseek-v4-flash" say nothing
        # reliable about a model's real capacity.
        return declared
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


def get_tool_payload_policy(config: dict[str, Any]) -> dict[str, int]:
    cls = derive_tool_payload_class(config)
    policy = dict(TOOL_PAYLOAD_POLICIES[cls])
    from ..config._knobs import knobValue

    text_bytes = int(knobValue(f"payloads.text_inline_bytes_{cls}"))
    policy.update({
        "textInlineBytes": text_bytes,
        # Line caps are backstops; the byte budget is the binding
        # constraint (~20 bytes/line of short-lined text).
        "textInlineLines": max(1, text_bytes // 20),
        "textHeadLines": max(1, text_bytes // 80),
        "textTailLines": max(1, text_bytes // 40),
        "readInlineBytes": int(knobValue(f"payloads.read_inline_bytes_{cls}")),
        "globInlineBytes": int(knobValue(f"payloads.glob_inline_bytes_{cls}")),
        "grepInlineBytes": int(knobValue(f"payloads.grep_inline_bytes_{cls}")),
    })
    return policy


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
    args = {**(args or {}), **kwargs}
    policy = get_tool_payload_policy(args["config"])
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
        lines = _fit_lines_within_bytes(split_tool_text_lines(text)[: policy["textInlineLines"]], policy["textInlineBytes"])
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








