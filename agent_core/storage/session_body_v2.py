from __future__ import annotations

import json
import mmap
import os
import time
import uuid
from typing import Any

from ..contracts.session_records import SessionBodyCommitRecord, SessionBodyMessageRecord
from .persistence_metrics import (
    persistence_observers_active as _persistence_observers_active,
    record_persistence_io as _record_persistence_io,
)


def _message_entry(session_id: str, index: int, message: Any) -> SessionBodyMessageRecord:
    return {
        "type": "message",
        "session_id": session_id,
        "message_index": index,
        "message": message,
    }


def _commit_entry(
    session_id: str, commit_id: str, message_count: int
) -> SessionBodyCommitRecord:
    return {
        "type": "commit",
        "session_id": session_id,
        "commit_id": commit_id,
        "message_count": message_count,
    }


def _body_commit_id(raw: Any) -> str | None:
    body_ref = raw.get("bodyRef") if isinstance(raw, dict) else None
    value = body_ref.get("commitId") if isinstance(body_ref, dict) else None
    return value if isinstance(value, str) and value else None


def _read_body_sync(
    path: str, session_id: str, commit_id: str | None = None
) -> dict[str, Any]:
    return _scan_body_sync(path, session_id, commit_id)


def _scan_body_sync(
    path: str, session_id: str, commit_id: str | None = None
) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return _empty_scan(commit_id)
    return _scan_body_bytes(raw, session_id, commit_id)


def _empty_scan(commit_id: str | None) -> dict[str, Any]:
    return {
        "messages": [],
        "truncatedTail": False,
        "validBytes": 0,
        "commitFound": commit_id is None,
        "bodyAhead": False,
    }


def _scan_body_bytes(
    raw: bytes, session_id: str, commit_id: str | None = None
) -> dict[str, Any]:
    if not raw:
        return _empty_scan(commit_id)
    complete_end = len(raw) if raw.endswith(b"\n") else raw.rfind(b"\n") + 1
    complete = raw[:complete_end]
    try:
        payload = complete.rstrip(b"\n")
        entries = (
            json.loads(b"[" + payload.replace(b"\n", b",") + b"]")
            if payload
            else []
        )
        state = _apply_body_entries(entries, session_id, commit_id)
    except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _scan_body_fallback(raw, session_id, commit_id)
    return {
        **state,
        "truncatedTail": complete_end < len(raw),
        "validBytes": complete_end,
    }


def _apply_body_entries(
    entries: Any, session_id: str, commit_id: str | None
) -> dict[str, Any]:
    if not isinstance(entries, list):
        raise ValueError("Session body is not a JSONL record sequence.")
    messages: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if _is_commit_entry(entry, session_id):
            if entry["message_count"] != len(messages):
                raise ValueError("Session body commit marker count mismatch.")
            if commit_id is not None and entry["commit_id"] == commit_id:
                return {
                    "messages": messages,
                    "commitFound": True,
                    "bodyAhead": index + 1 < len(entries),
                }
            continue
        messages = _apply_body_entry(messages, entry, session_id)
    return {
        "messages": messages,
        "commitFound": commit_id is None,
        "bodyAhead": False,
    }


def _is_commit_entry(entry: Any, session_id: str) -> bool:
    if not isinstance(entry, dict) or entry.get("type") != "commit":
        return False
    valid = (
        entry.get("session_id") == session_id
        and isinstance(entry.get("commit_id"), str)
        and bool(entry["commit_id"])
        and isinstance(entry.get("message_count"), int)
        and not isinstance(entry.get("message_count"), bool)
        and entry["message_count"] >= 0
    )
    if not valid:
        raise ValueError("Invalid session body commit marker.")
    return True


def _apply_body_entry(
    messages: list[dict[str, Any]], entry: Any, session_id: str
) -> list[dict[str, Any]]:
    if not isinstance(entry, dict) or entry.get("session_id") != session_id:
        raise ValueError("Session body identity mismatch.")
    if entry.get("type") == "truncate":
        count = entry.get("message_count")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            or count > len(messages)
        ):
            raise ValueError("Invalid session body truncate marker.")
        del messages[count:]
        return messages
    index = entry.get("message_index")
    message = entry.get("message")
    if (
        entry.get("type") != "message"
        or not isinstance(index, int)
        or index < 0
        or not isinstance(message, dict)
    ):
        raise ValueError("Invalid session body message record.")
    if index > len(messages):
        raise ValueError("Non-contiguous session body message index.")
    del messages[index:]
    messages.append(message)
    return messages


def _scan_body_fallback(
    raw: bytes, session_id: str, commit_id: str | None
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    valid_bytes = 0
    offset = 0
    commit_found = commit_id is None
    body_ahead = False
    for raw_line in raw.splitlines(keepends=True):
        offset += len(raw_line)
        if not raw_line.strip():
            valid_bytes = offset
            continue
        try:
            entry = json.loads(raw_line.decode("utf-8"))
            if _is_commit_entry(entry, session_id):
                if entry["message_count"] != len(messages):
                    raise ValueError("Session body commit marker count mismatch.")
                valid_bytes = offset
                if commit_id is not None and entry["commit_id"] == commit_id:
                    commit_found = True
                    body_ahead = offset < len(raw)
                    break
                continue
            messages = _apply_body_entry(messages, entry, session_id)
        except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            break
        valid_bytes = offset
    return {
        "messages": messages,
        "truncatedTail": valid_bytes < len(raw),
        "validBytes": valid_bytes,
        "commitFound": commit_found,
        "bodyAhead": body_ahead,
    }


def _append_body_sync(
    path: str,
    entries: list[dict[str, Any]],
    previous_commit_id: str | None,
) -> None:
    started = time.perf_counter_ns() if _persistence_observers_active() else 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    encoded = _encode_entries(entries)
    session_id = str(entries[0]["session_id"])
    if previous_commit_id is None:
        committed_end = _scan_body_sync(path, session_id)["validBytes"]
    else:
        committed_end = _find_commit_end_sync(path, session_id, previous_commit_id)
        if committed_end is None:
            raise ValueError("Persisted session body commit marker is missing.")
    mode = "r+b" if os.path.exists(path) else "w+b"
    with open(path, mode) as handle:
        handle.truncate(committed_end)
        handle.seek(0, os.SEEK_END)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    if started:
        _record_persistence_io(
            "append", path, len(encoded), time.perf_counter_ns() - started
        )


def _find_commit_end_sync(
    path: str, session_id: str, commit_id: str
) -> int | None:
    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                return None
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as body:
                needle = commit_id.encode("utf-8")
                cursor = len(body)
                while True:
                    found = body.rfind(needle, 0, cursor)
                    if found < 0:
                        return None
                    line_start = body.rfind(b"\n", 0, found) + 1
                    line_end = body.find(b"\n", found)
                    if line_end < 0:
                        line_end = len(body)
                    try:
                        entry = json.loads(
                            body[line_start:line_end].decode("utf-8")
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        cursor = found
                        continue
                    if (
                        _is_commit_entry(entry, session_id)
                        and entry["commit_id"] == commit_id
                    ):
                        return min(len(body), line_end + 1)
                    cursor = found
    except FileNotFoundError:
        return None


def _replace_body_sync(path: str, entries: list[dict[str, Any]]) -> int:
    started = time.perf_counter_ns() if _persistence_observers_active() else 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    encoded = _encode_entries(entries)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    if started:
        _record_persistence_io(
            "write", path, len(encoded), time.perf_counter_ns() - started
        )
    return len(encoded)


def _encode_entries(entries: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries
    ).encode("utf-8")


__all__: list[str] = []
