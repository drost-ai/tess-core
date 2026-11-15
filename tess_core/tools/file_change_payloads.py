from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knob_value

    return knob_value(name)


import inspect
from typing import Any, Callable

from .._utils import pretty_json
from ..payloads.tools import _byte_len, _count_lines, _persist_artifact, _without_none_values
from .file_change_diff import (
    build_create_diff_document,
    build_edit_diff_document,
    build_write_diff_document,
)




async def build_write_file_change_payload(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    previous = args.get("previousContent")
    document = (
        build_create_diff_document(args["nextContent"])
        if previous is None
        else build_write_diff_document(previous, args["nextContent"])
    )
    return await _shape_file_change_payload(
        {
            **args,
            "tool": "Write",
            "action": "create" if previous is None else "update",
            "document": document,
        }
    )


async def build_edit_file_change_payload(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    document = build_edit_diff_document(
        {
            "beforeText": args["previousContent"],
            "afterText": args["nextContent"],
            "oldString": args["oldString"],
            "newString": args["newString"],
            "replaceAll": bool(args.get("replaceAll")),
        }
    )
    return await _shape_file_change_payload(
        {**args, "tool": "Edit", "action": "update", "document": document}
    )


async def _shape_file_change_payload(args: dict[str, Any]) -> dict[str, Any]:
    document = args["document"]
    original_bytes = _byte_len(document["diff"])
    original_lines = _count_lines(document["diff"])
    target_bytes = int(_knob("payloads.file_change_diff_target_bytes"))
    excerpt = _build_diff_excerpt(
        document["diff"], target_bytes, int(_knob("payloads.file_change_diff_max_lines"))
    )
    artifact = None
    artifact_unavailable = False

    async def ensure_artifact() -> dict[str, Any] | None:
        nonlocal artifact, artifact_unavailable
        if artifact is not None or artifact_unavailable:
            return artifact
        writer: Callable[[dict[str, Any]], Any] = args.get("artifactWriter") or _persist_artifact
        try:
            pending = writer(
                {
                    "sessionId": args["sessionId"],
                    "toolName": args["tool"],
                    "family": "generic",
                    "content": document["diff"],
                    "contentType": "text",
                }
            )
            artifact = await pending if inspect.isawaitable(pending) else pending
        except Exception:
            artifact_unavailable = True
        return artifact

    if excerpt["truncated"]:
        await ensure_artifact()

    payload = _assemble_payload(
        args,
        excerpt["text"],
        artifact,
        artifact_unavailable,
        original_bytes,
        original_lines,
    )
    while _byte_len(pretty_json(payload)) > int(_knob("payloads.file_change_result_max_bytes")):
        await ensure_artifact()
        target_bytes = int(target_bytes * 0.65)
        excerpt = (
            {"text": "(diff omitted; see artifact)", "truncated": True}
            if target_bytes < 256
            else _build_diff_excerpt(
                document["diff"], target_bytes, int(_knob("payloads.file_change_diff_max_lines"))
            )
        )
        payload = _assemble_payload(
            args,
            excerpt["text"],
            artifact,
            artifact_unavailable,
            original_bytes,
            original_lines,
        )
        if target_bytes < 256:
            break

    if _byte_len(pretty_json(payload)) > int(_knob("payloads.file_change_result_max_bytes")):
        raise RuntimeError(
            "Unable to fit file-change result within the serialized payload limit."
        )
    return payload


def _assemble_payload(
    args: dict[str, Any],
    diff: str,
    artifact: dict[str, Any] | None,
    artifact_unavailable: bool,
    original_bytes: int,
    original_lines: int,
) -> dict[str, Any]:
    document = args["document"]
    truncated = diff != document["diff"]
    file_bytes = _byte_len(args["nextContent"])
    return _without_none_values(
        {
            "type": "file_change",
            "tool": args["tool"],
            "action": args["action"],
            "filePath": args["filePath"],
            "bytes": file_bytes,
            "fileBytes": file_bytes,
            "addedLines": document["addedLines"],
            "removedLines": document["removedLines"],
            "diffStrategy": document["strategy"],
            "note": args.get("note"),
            "warning": "Full diff artifact could not be persisted; inline diff is bounded."
            if artifact_unavailable
            else None,
            "diff": diff,
            "diffView": {
                "strategy": "inline_head_tail" if truncated else "inline_full",
                "truncated": truncated,
                "originalBytes": original_bytes,
                "originalLines": original_lines,
                "returnedBytes": _byte_len(diff),
                "returnedLines": _count_lines(diff),
                "artifact": artifact,
                "artifactUnavailable": True if artifact_unavailable else None,
            },
        }
    )


def _build_diff_excerpt(full_diff: str, max_bytes: int, max_lines: int) -> dict[str, Any]:
    lines = full_diff.replace("\r\n", "\n").split("\n")
    if len(lines) <= max_lines and _byte_len(full_diff) <= max_bytes:
        return {"text": full_diff, "truncated": False}

    head_count = min((max_lines - 1) // 2, len(lines))
    tail_count = min(max_lines - 1 - head_count, len(lines) - head_count)

    def render() -> str:
        omitted = max(0, len(lines) - head_count - tail_count)
        return "\n".join(
            [
                *lines[:head_count],
                f"...[omitted {omitted} diff lines]...",
                *lines[len(lines) - tail_count :],
            ]
        )

    text = render()
    while _byte_len(text) > max_bytes and (head_count > 1 or tail_count > 1):
        if head_count >= tail_count and head_count > 1:
            head_count -= 1
        elif tail_count > 1:
            tail_count -= 1
        text = render()
    if _byte_len(text) > max_bytes:
        text = f"...[diff omitted: {len(lines)} lines; see artifact]..."
    return {"text": text, "truncated": True}
