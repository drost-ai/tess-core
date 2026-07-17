from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from .._utils import pretty_json
from ..notebook_payload import shapeNotebookPayload
from ..payload_cursor import encodePayloadCursor, fingerprintPayloadSource, resolvePayloadCursor
from ..text_file_window import readTextFileWindow
from ..tool_payloads import getToolPayloadPolicy, shapeReadPayload, splitToolTextLines
from .definitions import ToolDefinition
from .file_change_payloads import (
    build_edit_file_change_payload,
    build_write_file_change_payload,
)
from .paths import _authorize_path, _display_path
from .permissions import _ensure_tool_permission
from .schemas import _tool_description, _tool_schema
from .validation import _require_string


def _read_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context)
        ext = Path(file_path).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            data = Path(file_path).read_bytes()
            return {"content": pretty_json({"type": "image", "file": {"filePath": file_path, "base64": base64.b64encode(data).decode(), "mime": mimetypes.guess_type(file_path)[0] or "image/jpeg", "originalSize": len(data)}})}
        if ext == ".pdf":
            data = Path(file_path).read_bytes()
            return {"content": pretty_json({"type": "pdf", "file": {"filePath": file_path, "base64": base64.b64encode(data).decode(), "originalSize": len(data)}})}
        if ext == ".ipynb":
            before_stat = os.stat(file_path)
            if before_stat.st_size > 16 * 1024 * 1024:
                raise ValueError(
                    f"Notebook exceeds the acquisition limit ({before_stat.st_size} > {16 * 1024 * 1024} bytes)."
                )
            data = Path(file_path).read_bytes()
            if len(data) > 16 * 1024 * 1024:
                raise ValueError("Notebook exceeds the acquisition limit.")
            after_stat = os.stat(file_path)
            notebook = json.loads(data.decode("utf-8"))
            snapshot = _file_snapshot(before_stat)
            source_fingerprint = fingerprintPayloadSource(
                [
                    "read_notebook",
                    file_path,
                    str(snapshot["dev"]),
                    str(snapshot["ino"]),
                    str(snapshot["size"]),
                    str(snapshot["mtimeNs"]),
                ]
            )
            cursor = resolvePayloadCursor(
                family="read_notebook",
                sourceFingerprint=source_fingerprint,
                cursor=input.get("cursor"),
                legacyOffset=input.get("offset") if "offset" in input else None,
                legacyBase=0,
            )
            if any(
                cursor.get(key) is not None
                for key in (
                    "byteOffset",
                    "sourceByteOffset",
                    "recordBytes",
                    "snapshotFingerprint",
                )
            ):
                raise ValueError("Notebook cursors must point to a cell boundary.")
            payload = await shapeNotebookPayload(
                config=context["config"],
                sessionId=context["session"]["id"],
                filePath=file_path,
                notebook=notebook,
                sourceBytes=len(data),
                sourceFingerprint=source_fingerprint,
                offset=cursor["recordOffset"],
                requestedLimit=input.get("limit"),
                changedDuringRead=_file_snapshot(after_stat) != snapshot,
            )
            return {"content": pretty_json(payload)}
        stat = os.stat(file_path)
        if not Path(file_path).is_file():
            raise ValueError("Read supports regular files only.")
        with open(file_path, "rb") as handle:
            prefix = handle.read(8_192)
        if _looks_binary(prefix):
            return {
                "content": pretty_json(
                    {
                        "type": "binary",
                        "file": {
                            "filePath": file_path,
                            "bytes": stat.st_size,
                            "rawBytesInlined": False,
                            "note": "Binary file content is not returned inline.",
                        },
                    }
                )
            }
        snapshot = _file_snapshot(stat)
        source_fingerprint = fingerprintPayloadSource(
            [
                "read_text",
                file_path,
                str(snapshot["dev"]),
                str(snapshot["ino"]),
                str(snapshot["size"]),
                str(snapshot["mtimeNs"]),
            ]
        )
        cursor = resolvePayloadCursor(
            family="read_text",
            sourceFingerprint=source_fingerprint,
            cursor=input.get("cursor"),
            legacyOffset=input.get("offset") if "offset" in input else None,
            legacyBase=1,
        )
        if cursor.get("snapshotFingerprint") is not None:
            raise ValueError("Read text cursors cannot carry a search snapshot fingerprint.")
        policy = getToolPayloadPolicy(context["config"])
        requested_limit = input.get("limit")
        window = await readTextFileWindow(
            filePath=file_path,
            offset=cursor["recordOffset"] + 1,
            byteOffset=cursor.get("byteOffset"),
            sourceByteOffset=cursor.get("sourceByteOffset"),
            recordBytes=cursor.get("recordBytes"),
            requestedLimit=requested_limit or policy["readDefaultLines"],
            maxLines=policy["readInlineLines"],
            maxBytes=policy["readInlineBytes"],
            expectedSnapshot=snapshot,
            signal=context.get("abortSignal"),
        )
        next_position = window.get("nextPosition")
        next_cursor = (
            encodePayloadCursor(
                family="read_text",
                sourceFingerprint=source_fingerprint,
                position=next_position,
            )
            if next_position
            else None
        )
        changed = window["changedDuringRead"]
        payload = {
            "type": "text",
            "file": {
                "filePath": file_path,
                "content": window["content"],
                "numLines": window["returnedLines"],
                "startLine": window["startLine"],
                **({"totalLines": window["totalLines"]} if "totalLines" in window else {}),
                "totalLinesAtLeast": window["totalLinesAtLeast"],
                "totalLinesKnown": window["totalLinesKnown"],
                "sourceBytes": window["sourceBytes"],
                "scannedBytes": window["scannedBytes"],
                "selectedBytes": window["returnedBytes"],
                "truncated": window["truncated"],
                "requestedLimit": window["requestedLimit"],
                **({"nextCursor": next_cursor} if next_cursor else {}),
                **({"nextOffset": window["nextOffset"]} if "nextOffset" in window else {}),
                **(
                    {
                        "cursorCompatibility": {
                            "legacyNextOffsetSkipsRemainder": True,
                            "note": "Use nextCursor to continue this line byte-for-byte; nextOffset advances to the following line.",
                        }
                    }
                    if next_position and next_position.get("byteOffset")
                    else {}
                ),
                **({"oversizedLine": window["oversizedLine"]} if "oversizedLine" in window else {}),
                **({"changedDuringRead": True} if changed else {}),
                **(
                    {
                        "consistencyNote": "The file changed while it was read; this page came from the opened file snapshot."
                    }
                    if changed
                    else {}
                ),
                "recovery": {"type": "source_file", "filePath": file_path},
                "transcriptView": {
                    "strategy": "windowed_file_read" if window["truncated"] else "inline_full",
                    "truncated": window["truncated"],
                    "originalBytes": window["sourceBytes"],
                    "originalLines": window.get("totalLines"),
                    "returnedBytes": window["returnedBytes"],
                    "returnedLines": window["returnedLines"],
                },
            },
        }
        return {"content": pretty_json(payload)}

    return ToolDefinition("Read", _tool_description("Read"), _tool_schema("Read"), handler)


def _looks_binary(value: bytes) -> bool:
    if b"\0" in value:
        return True
    if not value:
        return False
    controls = sum(1 for byte in value if byte < 0x09 or 0x0D < byte < 0x20)
    return controls / len(value) > 0.1


def _file_snapshot(stat: os.stat_result) -> dict[str, int]:
    return {
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "dev": stat.st_dev,
        "ino": stat.st_ino,
    }


def _write_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context, True)
        content = _require_string(input.get("content"), "content")
        await _ensure_tool_permission(context, "Write", "edit", f"Write {_display_path(context, file_path)} ({len(content)} bytes)", path=file_path)
        previous = Path(file_path).read_text(encoding="utf-8") if Path(file_path).exists() else None
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        Path(file_path).write_text(content, encoding="utf-8")
        payload = await build_write_file_change_payload(
            config=context["config"],
            sessionId=context["session"]["id"],
            filePath=file_path,
            previousContent=previous,
            nextContent=content,
        )
        return {"content": pretty_json(payload)}

    return ToolDefinition("Write", _tool_description("Write"), _tool_schema("Write"), handler)


def _edit_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context)
        old = _require_string(input.get("old_string"), "old_string")
        new = _require_string(input.get("new_string"), "new_string")
        replace_all = bool(input.get("replace_all"))
        await _ensure_tool_permission(context, "Edit", "edit", f"Edit {_display_path(context, file_path)}", path=file_path)
        original = Path(file_path).read_text(encoding="utf-8")
        if old not in original:
            return {"content": f"String not found in {file_path}", "isError": True}
        updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
        Path(file_path).write_text(updated, encoding="utf-8")
        payload = await build_edit_file_change_payload(
            config=context["config"],
            sessionId=context["session"]["id"],
            filePath=file_path,
            previousContent=original,
            nextContent=updated,
            oldString=old,
            newString=new,
            replaceAll=replace_all,
            note="Replaced all occurrences."
            if replace_all
            else "Replaced the first matching occurrence.",
        )
        return {"content": pretty_json(payload)}

    return ToolDefinition("Edit", _tool_description("Edit"), _tool_schema("Edit"), handler)
