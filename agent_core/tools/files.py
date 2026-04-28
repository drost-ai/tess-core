from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from .._utils import pretty_json
from ..tool_payloads import shapeReadPayload, splitToolTextLines
from .definitions import ToolDefinition
from .diffs import _file_change_payload
from .paths import _authorize_path, _display_path
from .permissions import _ensure_tool_permission
from .schemas import _tool_description, _tool_schema
from .validation import _require_string


def _read_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context)
        data = Path(file_path).read_bytes()
        ext = Path(file_path).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            return {"content": pretty_json({"type": "image", "file": {"filePath": file_path, "base64": base64.b64encode(data).decode(), "mime": mimetypes.guess_type(file_path)[0] or "image/jpeg", "originalSize": len(data)}})}
        if ext == ".pdf":
            return {"content": pretty_json({"type": "pdf", "file": {"filePath": file_path, "base64": base64.b64encode(data).decode(), "originalSize": len(data)}})}
        if ext == ".ipynb":
            notebook = json.loads(data.decode("utf-8"))
            return {"content": pretty_json({"type": "notebook", "file": {"filePath": file_path, "cells": notebook.get("cells", [])}})}
        text = data.decode("utf-8", errors="replace")
        payload = await shapeReadPayload(
            config=context["config"],
            sessionId=context["session"]["id"],
            filePath=file_path,
            lines=splitToolTextLines(text),
            offset=int(input.get("offset") or 1),
            requestedLimit=input.get("limit"),
        )
        return {"content": pretty_json(payload)}

    return ToolDefinition("Read", _tool_description("Read"), _tool_schema("Read"), handler)


def _write_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context, True)
        content = _require_string(input.get("content"), "content")
        await _ensure_tool_permission(context, "Write", "edit", f"Write {_display_path(context, file_path)} ({len(content)} bytes)")
        previous = Path(file_path).read_text(encoding="utf-8") if Path(file_path).exists() else None
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        Path(file_path).write_text(content, encoding="utf-8")
        return {"content": pretty_json(_file_change_payload("Write", file_path, previous, content))}

    return ToolDefinition("Write", _tool_description("Write"), _tool_schema("Write"), handler)


def _edit_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context)
        old = _require_string(input.get("old_string"), "old_string")
        new = _require_string(input.get("new_string"), "new_string")
        replace_all = bool(input.get("replace_all"))
        await _ensure_tool_permission(context, "Edit", "edit", f"Edit {_display_path(context, file_path)}")
        original = Path(file_path).read_text(encoding="utf-8")
        if old not in original:
            return {"content": f"String not found in {file_path}", "isError": True}
        updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
        Path(file_path).write_text(updated, encoding="utf-8")
        return {"content": pretty_json(_file_change_payload("Edit", file_path, original, updated, {"note": "Replaced all occurrences." if replace_all else "Replaced the first matching occurrence."}))}

    return ToolDefinition("Edit", _tool_description("Edit"), _tool_schema("Edit"), handler)
