from __future__ import annotations

import asyncio
import math
import os
import stat
from typing import Any, BinaryIO

from .utf8_bounds import (
    normalize_utf8_string,
    slice_utf8_prefix,
    slice_utf8_suffix,
    utf8_byte_length,
)


GENERIC_ONE_SHOT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_STREAM_CHUNK_BYTES = 64 * 1024


class AcquisitionLimitError(RuntimeError):
    code = "acquisition_limit"

    def __init__(self, label: str, measured_bytes: int, limit_bytes: int):
        super().__init__(
            f"{label} exceeds the acquisition limit ({measured_bytes} > {limit_bytes} bytes)."
        )
        self.measuredBytes = measured_bytes
        self.limitBytes = limit_bytes


def _normalize_byte_limit(value: int | float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0
    return math.floor(value)


def _file_snapshot(value: os.stat_result) -> tuple[int, int, int, int]:
    return (int(value.st_dev), int(value.st_ino), int(value.st_size), int(value.st_mtime_ns))


def _require_regular_file(value: os.stat_result) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("Read supports regular files only.")


def _read_exact_at_most(handle: BinaryIO, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining > 0:
        chunk = handle.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_file_tail_bounded_sync(file_path: str, max_bytes: int | float) -> dict[str, Any]:
    limit = _normalize_byte_limit(max_bytes)
    path_stat = os.stat(file_path)
    _require_regular_file(path_stat)
    path_snapshot = _file_snapshot(path_stat)
    with open(file_path, "rb", buffering=0) as handle:
        opened_stat = os.fstat(handle.fileno())
        _require_regular_file(opened_stat)
        before_snapshot = _file_snapshot(opened_stat)
        source_bytes = int(opened_stat.st_size)
        if source_bytes <= 0 or limit == 0:
            return {
                "text": "",
                "originalBytes": source_bytes,
                "returnedBytes": 0,
                "truncated": source_bytes > 0,
                "changed": path_snapshot != before_snapshot,
            }
        bytes_to_read = min(source_bytes, limit + 4)
        start = max(0, source_bytes - bytes_to_read)
        handle.seek(start)
        raw = _read_exact_at_most(handle, bytes_to_read)
        decoded = normalize_utf8_string(raw.decode("utf-8", errors="replace"))
        text = slice_utf8_suffix(decoded, limit)
        after_snapshot = _file_snapshot(os.fstat(handle.fileno()))
        return {
            "text": text,
            "originalBytes": source_bytes,
            "returnedBytes": utf8_byte_length(text),
            "truncated": start > 0 or utf8_byte_length(decoded) > limit,
            "changed": path_snapshot != before_snapshot or before_snapshot != after_snapshot,
        }


def _read_file_prefix_bounded_sync(
    file_path: str, max_bytes: int | float, reject_symlink: bool = False
) -> dict[str, Any]:
    limit = _normalize_byte_limit(max_bytes)
    path_stat = os.lstat(file_path) if reject_symlink else os.stat(file_path)
    if reject_symlink and stat.S_ISLNK(path_stat.st_mode):
        raise ValueError("Read refuses symbolic links.")
    _require_regular_file(path_stat)
    path_snapshot = _file_snapshot(path_stat)
    if reject_symlink:
        descriptor = os.open(file_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fdopen(descriptor, "rb", buffering=0)
    else:
        opened = open(file_path, "rb", buffering=0)
    with opened as handle:
        opened_stat = os.fstat(handle.fileno())
        _require_regular_file(opened_stat)
        before_snapshot = _file_snapshot(opened_stat)
        source_bytes = int(opened_stat.st_size)
        raw = _read_exact_at_most(handle, min(source_bytes, limit + 4))
        after_snapshot = _file_snapshot(os.fstat(handle.fileno()))
        decoded = normalize_utf8_string(raw.decode("utf-8", errors="replace"))
        text = slice_utf8_prefix(decoded, limit)
        return {
            "text": text,
            "originalBytes": source_bytes,
            "returnedBytes": utf8_byte_length(text),
            "truncated": source_bytes > limit,
            "changed": path_snapshot != before_snapshot or before_snapshot != after_snapshot,
        }


async def read_file_prefix_bounded(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    file_path = values.get("filePath") or values.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("filePath is required.")
    max_bytes = values.get("maxBytes", values.get("max_bytes", 0))
    reject_symlink = bool(
        values.get("rejectSymlink", values.get("reject_symlink", False))
    )
    return await asyncio.to_thread(
        _read_file_prefix_bounded_sync, file_path, max_bytes, reject_symlink
    )


async def read_file_tail_bounded(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    file_path = values.get("filePath") or values.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("filePath is required.")
    max_bytes = values.get("maxBytes", values.get("max_bytes", 0))
    return await asyncio.to_thread(_read_file_tail_bounded_sync, file_path, max_bytes)


def read_response_text_bounded_sync(
    response: Any,
    *,
    maxBytes: int = GENERIC_ONE_SHOT_MAX_BYTES,
    label: str = "Response",
) -> dict[str, Any]:
    limit = _normalize_byte_limit(maxBytes)
    declared = _response_content_length(response)
    if declared is not None and declared > limit:
        raise AcquisitionLimitError(label, declared, limit)
    chunks: list[bytes] = []
    measured = 0
    while True:
        remaining = max(0, limit + 1 - measured)
        if remaining == 0:
            raise AcquisitionLimitError(label, measured, limit)
        chunk = response.read(min(DEFAULT_STREAM_CHUNK_BYTES, remaining))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        measured += len(chunk)
        if measured > limit:
            raise AcquisitionLimitError(label, measured, limit)
        chunks.append(chunk)
    raw = b"".join(chunks)
    return {
        "text": normalize_utf8_string(raw.decode("utf-8", errors="replace")),
        "bytes": measured,
        "truncated": False,
    }


def _response_content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    value = headers.get("content-length") if hasattr(headers, "get") else None
    if value is None and hasattr(response, "getheader"):
        value = response.getheader("content-length")
    text = str(value or "")
    return int(text) if text.isdigit() else None


