from __future__ import annotations

import asyncio
import os
from typing import Any

from .utf8_bounds import normalizeUtf8String, sliceUtf8Prefix, utf8ByteLength


DEFAULT_STREAM_CHUNK_BYTES = 64 * 1024


async def readTextFileWindow(**kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(_read_text_file_window_sync, kwargs)


def _read_text_file_window_sync(args: dict[str, Any]) -> dict[str, Any]:
    start_line = _positive(args.get("offset"), 1)
    requested_limit = _positive(args.get("requestedLimit"), 1)
    max_lines = _positive(args.get("maxLines"), 1)
    page_line_limit = min(requested_limit, max_lines)
    max_bytes = max(1, int(args.get("maxBytes", 1)))
    start_byte_offset = _non_negative(args.get("byteOffset"), "Read cursor offsets")
    source_byte_offset = (
        None
        if args.get("sourceByteOffset") is None
        else _non_negative(args["sourceByteOffset"], "Read cursor offsets")
    )
    expected_record_bytes = (
        None
        if args.get("recordBytes") is None
        else _non_negative(args["recordBytes"], "Read cursor offsets")
    )
    if source_byte_offset is not None and source_byte_offset < start_byte_offset:
        raise ValueError("Read cursor source byte offset precedes its line byte offset.")
    if expected_record_bytes is not None and start_byte_offset > expected_record_bytes:
        raise ValueError("Read cursor byte offset exceeds its recorded line size.")

    with open(args["filePath"], "rb") as handle:
        before = _snapshot(os.fstat(handle.fileno()))
        if source_byte_offset is not None and source_byte_offset > before["size"]:
            raise ValueError(
                f"Read cursor source byte offset {source_byte_offset} exceeds file size {before['size']}."
            )
        selected: list[str] = []
        selected_bytes = 0
        current_line = start_line if source_byte_offset is not None else 1
        current_line_bytes = start_byte_offset if source_byte_offset is not None else 0
        current_line_absolute_start = (source_byte_offset or 0) - current_line_bytes
        stored_line_bytes = 0
        stored_parts: list[bytes] = []
        position = source_byte_offset or 0
        handle.seek(position)
        scanned_bytes = 0
        stopped = False
        reached_eof = False
        oversized_line: dict[str, Any] | None = None
        next_position: dict[str, Any] | None = None

        def reset_line() -> None:
            nonlocal current_line_bytes, stored_line_bytes
            current_line_bytes = 0
            stored_line_bytes = 0
            stored_parts.clear()

        def store_segment(value: bytes, segment_line_offset: int) -> None:
            nonlocal stored_line_bytes
            if current_line < start_line or stored_line_bytes >= max_bytes + 4:
                return
            required = start_byte_offset if current_line == start_line else 0
            segment_end = segment_line_offset + len(value)
            if segment_end <= required:
                return
            after_offset = value[max(0, required - segment_line_offset) :]
            kept = after_offset[: max_bytes + 4 - stored_line_bytes]
            if kept:
                stored_parts.append(kept)
                stored_line_bytes += len(kept)

        def stop_inside_line(raw: str, original_bytes: int | None = None) -> None:
            nonlocal selected_bytes, stopped, next_position, oversized_line
            applied = start_byte_offset if current_line == start_line else 0
            separator_bytes = 1 if selected else 0
            available = max(0, max_bytes - selected_bytes - separator_bytes)
            fitted = sliceUtf8Prefix(raw, available)
            fitted_bytes = utf8ByteLength(fitted)
            skipped_bytes = 0
            if fitted_bytes > 0:
                selected.append(fitted)
                selected_bytes += separator_bytes + fitted_bytes
            elif not selected and raw:
                skipped_bytes = utf8ByteLength(next(iter(raw), ""))
            next_byte_offset = applied + fitted_bytes + skipped_bytes
            next_position = {
                "recordOffset": current_line - 1,
                "byteOffset": next_byte_offset,
                "sourceByteOffset": current_line_absolute_start + next_byte_offset,
                **({"recordBytes": original_bytes} if original_bytes is not None else {}),
            }
            oversized_line = {
                "line": current_line,
                **({"originalBytes": original_bytes} if original_bytes is not None else {}),
                "originalBytesAtLeast": original_bytes
                if original_bytes is not None
                else max(current_line_bytes, next_byte_offset + 1),
                "startByteOffset": applied,
                "returnedBytes": fitted_bytes,
                **({"skippedBytes": skipped_bytes} if skipped_bytes else {}),
                "nextByteOffset": next_byte_offset,
                "continuationAvailable": True,
                "remainderSkipped": True,
            }
            stopped = True

        def maybe_stop_before_line_end() -> None:
            if current_line < start_line or stopped or expected_record_bytes is None:
                return
            separator_bytes = 1 if selected else 0
            available = max(0, max_bytes - selected_bytes - separator_bytes)
            applied = start_byte_offset if current_line == start_line else 0
            if expected_record_bytes - applied <= available or stored_line_bytes <= available:
                return
            raw = normalizeUtf8String(b"".join(stored_parts).decode("utf-8", errors="replace"))
            stop_inside_line(raw, expected_record_bytes)

        def finish_line(at_eof: bool, next_line_start: int) -> None:
            nonlocal current_line, current_line_absolute_start, selected_bytes
            nonlocal stopped, next_position
            if current_line < start_line:
                current_line += 1
                current_line_absolute_start = next_line_start
                reset_line()
                return
            if len(selected) >= page_line_limit:
                next_position = {
                    "recordOffset": current_line - 1,
                    "sourceByteOffset": current_line_absolute_start,
                }
                stopped = True
                return
            stored = b"".join(stored_parts)
            had_cr = stored.endswith(b"\r")
            if had_cr:
                stored = stored[:-1]
            raw = normalizeUtf8String(stored.decode("utf-8", errors="replace"))
            original_line_bytes = current_line_bytes - 1 if current_line_bytes > 0 and had_cr else current_line_bytes
            applied = start_byte_offset if current_line == start_line else 0
            if applied > original_line_bytes:
                raise ValueError(
                    f"Read cursor byte offset {applied} exceeds line {current_line} size {original_line_bytes}."
                )
            separator_bytes = 1 if selected else 0
            available = max(0, max_bytes - selected_bytes - separator_bytes)
            fitted = sliceUtf8Prefix(raw, available)
            fitted_bytes = utf8ByteLength(fitted)
            if fitted_bytes < original_line_bytes - applied:
                stop_inside_line(raw, original_line_bytes)
                return
            selected.append(fitted)
            selected_bytes += separator_bytes + fitted_bytes
            current_line += 1
            current_line_absolute_start = next_line_start
            reset_line()
            if not at_eof and (len(selected) >= page_line_limit or selected_bytes >= max_bytes):
                stopped = True

        while not stopped:
            _throw_if_aborted(args.get("signal"))
            chunk_start = position
            chunk = handle.read(DEFAULT_STREAM_CHUNK_BYTES)
            if not chunk:
                reached_eof = True
                # The zero-byte line after a trailing newline IS finished and
                # counted: the TS peer counts it, the Read parity packs pin
                # it, and joining it back is what keeps content byte-faithful
                # to the file. (It deliberately disagrees with the search
                # sampler's line accounting.)
                finish_line(True, position)
                break
            position += len(chunk)
            scanned_bytes += len(chunk)
            segment_start = 0
            for index, byte in enumerate(chunk):
                if byte != 0x0A:
                    continue
                segment = chunk[segment_start:index]
                segment_line_offset = current_line_bytes
                current_line_bytes += len(segment)
                store_segment(segment, segment_line_offset)
                finish_line(False, chunk_start + index + 1)
                segment_start = index + 1
                if stopped:
                    break
            if not stopped and segment_start < len(chunk):
                segment = chunk[segment_start:]
                segment_line_offset = current_line_bytes
                current_line_bytes += len(segment)
                store_segment(segment, segment_line_offset)
                maybe_stop_before_line_end()

        after = _snapshot(os.fstat(handle.fileno()))
        total_lines_known = reached_eof and not stopped
        total_lines = max(1, current_line - 1) if total_lines_known else None
        if next_position is None and not total_lines_known and selected:
            next_position = {
                "recordOffset": current_line - 1,
                "sourceByteOffset": current_line_absolute_start,
            }
        next_offset = (
            next_position["recordOffset"] + (2 if next_position.get("byteOffset") else 1)
            if next_position
            else None
        )
        content = "\n".join(selected)
        expected = args.get("expectedSnapshot")
        return {
            "content": content,
            "startLine": start_line,
            "returnedLines": len(selected),
            "requestedLimit": requested_limit,
            **({"nextOffset": next_offset} if next_offset is not None else {}),
            **({"nextPosition": next_position} if next_position else {}),
            **({"totalLines": total_lines} if total_lines is not None else {}),
            "totalLinesAtLeast": total_lines or max(current_line, start_line),
            "totalLinesKnown": total_lines_known,
            "sourceBytes": before["size"],
            "scannedBytes": scanned_bytes,
            "returnedBytes": utf8ByteLength(content),
            "truncated": bool(next_position or oversized_line),
            **({"oversizedLine": oversized_line} if oversized_line else {}),
            "changedDuringRead": bool(
                (expected and not _same_snapshot(expected, before))
                or not _same_snapshot(before, after)
            ),
        }


def _snapshot(stat: os.stat_result) -> dict[str, int]:
    return {
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "dev": stat.st_dev,
        "ino": stat.st_ino,
    }


def _same_snapshot(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("size", "mtimeNs", "dev", "ino"))


def _positive(value: Any, fallback: int) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else fallback


def _non_negative(value: Any, label: str) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be non-negative safe integers.")
    return value


def _throw_if_aborted(signal_value: Any) -> None:
    aborted = (
        bool(signal_value.get("aborted"))
        if isinstance(signal_value, dict)
        else bool(getattr(signal_value, "aborted", False))
    )
    if aborted:
        error = RuntimeError("Tool execution cancelled")
        error.name = "AbortError"  # type: ignore[attr-defined]
        raise error


