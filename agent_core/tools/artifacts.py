from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knobValue

    return knobValue(name)


import json
import os
import stat
from typing import Any

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult
from ..runtime.identifiers import parse_artifact_id
from ..storage import tool_artifact_data_path, tool_artifact_meta_path
from ..payloads.result_guard import (
    non_artifact_tool_result_max_bytes,
    serializedContentBytes,
)
from .definitions import ToolDefinition
from .schemas import builtin_tool


MAX_ARTIFACT_PAGE_BYTES = 24_000
MIN_ARTIFACT_PAGE_BYTES = 256


def _read_tool_artifact() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        session = context.get("session")
        session_id = (
            str(session.get("id") or "")
            if isinstance(session, dict)
            else ""
        )
        if not session_id:
            return _error_result(
                "artifact_session_unavailable",
                "The active session identity is unavailable.",
            )
        try:
            artifact_id = parse_artifact_id(input.get("artifact_id"))
            offset = _bounded_nonnegative_integer(
                input.get("offset_bytes"),
                default=0,
                name="offset_bytes",
            )
            requested = _bounded_page_size(input.get("max_bytes"))
        except ValueError as error:
            return _error_result("artifact_request_invalid", str(error))

        try:
            metadata = _read_registered_artifact_metadata(
                tool_artifact_meta_path(session_id, artifact_id)
            )
        except FileNotFoundError:
            metadata = None
        except (OSError, ValueError):
            return _error_result(
                "artifact_metadata_invalid",
                "The registered tool artifact metadata could not be securely read.",
                artifact_id=artifact_id,
                offset=offset,
            )
        if not isinstance(metadata, dict):
            return _error_result(
                "artifact_not_found",
                "No tool artifact with that id belongs to the active session.",
                artifact_id=artifact_id,
                offset=offset,
            )
        if metadata.get("sessionId") != session_id:
            return _error_result(
                "artifact_authority_mismatch",
                "The tool artifact does not belong to the active session.",
                artifact_id=artifact_id,
                offset=offset,
            )

        extension = ".json" if metadata.get("contentType") == "json" else ".txt"
        expected_path = tool_artifact_data_path(session_id, artifact_id, extension)
        recorded_path = metadata.get("filePath")
        if (
            not isinstance(recorded_path, str)
            or os.path.abspath(recorded_path) != os.path.abspath(expected_path)
        ):
            return _error_result(
                "artifact_metadata_invalid",
                "The tool artifact metadata does not identify its registered data file.",
                artifact_id=artifact_id,
                offset=offset,
            )

        try:
            page = _read_registered_artifact_page(
                expected_path,
                offset=offset,
                requested=requested,
                expected_bytes=metadata.get("bytes"),
            )
        except FileNotFoundError:
            return _error_result(
                "artifact_not_found",
                "The registered tool artifact data is unavailable.",
                artifact_id=artifact_id,
                offset=offset,
            )
        except (OSError, ValueError) as error:
            return _error_result(
                "artifact_read_failed",
                str(error),
                artifact_id=artifact_id,
                offset=offset,
            )

        payload = _page_payload(
            artifact_id=artifact_id,
            metadata=metadata,
            offset=offset,
            page=page,
        )
        while (
            serializedContentBytes(json.dumps(payload, ensure_ascii=False))
            > non_artifact_tool_result_max_bytes()
            and page["returnedBytes"] > MIN_ARTIFACT_PAGE_BYTES
        ):
            requested = max(
                MIN_ARTIFACT_PAGE_BYTES,
                page["returnedBytes"] // 2,
            )
            page = _read_registered_artifact_page(
                expected_path,
                offset=offset,
                requested=requested,
                expected_bytes=metadata.get("bytes"),
            )
            payload = _page_payload(
                artifact_id=artifact_id,
                metadata=metadata,
                offset=offset,
                page=page,
            )
        serialized = json.dumps(payload, indent=2, ensure_ascii=False)
        if serializedContentBytes(serialized) > non_artifact_tool_result_max_bytes():
            return _error_result(
                "artifact_page_encoding_too_large",
                "The requested artifact page could not be represented safely.",
                artifact_id=artifact_id,
                offset=offset,
            )
        return {
            "content": serialized,
            "meta": {
                "agentCoreArtifactPage": True,
                "artifactId": artifact_id,
                "offsetBytes": offset,
                "nextOffsetBytes": page["nextOffsetBytes"],
                "complete": page["complete"],
            },
        }

    return builtin_tool("ReadToolArtifact", handler)


def _read_registered_artifact_metadata(file_path: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(file_path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > 64 * 1024:
            raise ValueError("Invalid tool artifact metadata file.")
        chunks: list[bytes] = []
        remaining = file_stat.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != file_stat.st_size:
            raise ValueError("Tool artifact metadata changed while being read.")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid tool artifact metadata.")
        return value
    finally:
        os.close(descriptor)


def _read_registered_artifact_page(
    file_path: str,
    *,
    offset: int,
    requested: int,
    expected_bytes: Any,
) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(file_path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("The registered tool artifact is not a regular file.")
        if (
            isinstance(expected_bytes, int)
            and expected_bytes >= 0
            and file_stat.st_size != expected_bytes
        ):
            raise ValueError("The registered tool artifact changed after persistence.")
        if offset > file_stat.st_size:
            raise ValueError("offset_bytes exceeds the artifact size.")
        if offset < file_stat.st_size:
            first = os.pread(descriptor, 1, offset)
            if first and first[0] & 0xC0 == 0x80:
                raise ValueError("offset_bytes must be a UTF-8 character boundary.")
        raw = os.pread(descriptor, requested + 4, offset)
        selected = raw[:requested]
        try:
            text = selected.decode("utf-8")
        except UnicodeDecodeError as error:
            if error.reason != "unexpected end of data":
                raise ValueError("The registered tool artifact is not valid UTF-8.") from error
            selected = selected[: error.start]
            text = selected.decode("utf-8")
        returned = len(selected)
        next_offset = offset + returned
        return {
            "content": text,
            "returnedBytes": returned,
            "totalBytes": file_stat.st_size,
            "nextOffsetBytes": next_offset,
            "complete": next_offset >= file_stat.st_size,
        }
    finally:
        os.close(descriptor)


def _page_payload(
    *,
    artifact_id: str,
    metadata: dict[str, Any],
    offset: int,
    page: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "tool_artifact_page",
        "artifactId": artifact_id,
        "offsetBytes": offset,
        "returnedBytes": page["returnedBytes"],
        "totalBytes": page["totalBytes"],
        "nextOffsetBytes": page["nextOffsetBytes"],
        "complete": page["complete"],
        "contentType": metadata.get("contentType") or "text",
        "content": page["content"],
    }


def _error_result(
    code: str,
    message: str,
    *,
    artifact_id: str | None = None,
    offset: int | None = None,
) -> ToolResult:
    return {
        "content": json.dumps(
            {
                "type": "tool_artifact_page_error",
                "code": code,
                "message": message[:1_000],
                **({"artifactId": artifact_id} if artifact_id else {}),
                **({"offsetBytes": offset} if offset is not None else {}),
            },
            indent=2,
            ensure_ascii=False,
        ),
        "isError": True,
        "meta": {
            "agentCoreArtifactPage": True,
            **({"artifactId": artifact_id} if artifact_id else {}),
            **({"offsetBytes": offset} if offset is not None else {}),
        },
    }


def _bounded_nonnegative_integer(
    value: Any,
    *,
    default: int,
    name: str,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a non-negative integer.") from None
    if parsed < 0 or (isinstance(value, float) and value != parsed):
        raise ValueError(f"{name} must be a non-negative integer.")
    return parsed


def _bounded_page_size(value: Any) -> int:
    parsed = _bounded_nonnegative_integer(
        value,
        default=int(_knob("tools.artifact_page_bytes")),
        name="max_bytes",
    )
    if parsed <= 0:
        raise ValueError("max_bytes must be a positive integer.")
    return min(MAX_ARTIFACT_PAGE_BYTES, parsed)
