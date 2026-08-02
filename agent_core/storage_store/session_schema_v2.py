from __future__ import annotations

import copy
import json
import os
import time
import uuid
from typing import Any, Callable

from .._media_persistence import sanitizeMediaForPersistence as _sanitize_media
from ..message_origin import isGenuineUserRequest as _is_genuine_user_request
from ..persistence_metrics import (
    persistenceObserversActive as _persistence_observers_active,
    recordPersistenceIo as _record_persistence_io,
)
from ..runtime_identifiers import parseSessionId
from .io_executor import runStorageIo as _run_storage_io
from .shared import *

SESSION_SCHEMA_V2 = 2
SESSION_BODY_FORMAT = "agent-core.session-messages.jsonl.v1"
_HEAD_FIELDS = {
    "schemaVersion", "messageCount", "bodyRef", "hasPersistableContent", "firstUserText"
}


class SessionSchemaVersionError(RuntimeError):
    def __init__(self, version: Any, maximum: int = SESSION_SCHEMA_V2):
        super().__init__(f"Session schema {version!r} is not supported (maximum {maximum}).")
        self.code = "session.schema_unsupported"
        self.version = version
        self.maximum = maximum


def sessionMessagesPath(sessionId: str) -> str:
    return os.path.join(getStateRoot(), "sessions", f"{parseSessionId(sessionId)}.messages.jsonl")


def assertSessionSchemaSupported(record: Any, maximum: int = SESSION_SCHEMA_V2) -> None:
    version = record.get("schemaVersion", 1) if isinstance(record, dict) else None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1 or version > maximum:
        raise SessionSchemaVersionError(version, maximum)


def isSessionHeadV2(record: Any) -> bool:
    return isinstance(record, dict) and record.get("schemaVersion") == SESSION_SCHEMA_V2


async def hydratePersistedSession(
    sessionId: str,
    raw: dict[str, Any] | None,
    *,
    repairHead: bool = True,
) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    if raw is None:
        return {"record": None, "changed": False, "reason": None}
    if not isinstance(raw, dict) or raw.get("id") != session_id:
        raise ValueError(f"Session record identity mismatch for {session_id}.")
    assertSessionSchemaSupported(raw)
    if not isSessionHeadV2(raw):
        if not isinstance(raw.get("messages"), list):
            raise ValueError(f"Legacy session {session_id} has no message list.")
        return {"record": copy.deepcopy(raw), "changed": False, "reason": None}
    _validate_v2_head(raw, session_id)
    body = await _run_storage_io(_read_body_sync, sessionMessagesPath(session_id), session_id)
    committed_count = raw["messageCount"]
    available_count = len(body["messages"])
    selected_count = min(committed_count, available_count)
    record = _hydrate_head(raw, body["messages"][:selected_count])
    reason = None
    changed = False
    if available_count < committed_count:
        reason = "body_shorter"
        changed = True
        if repairHead:
            repaired = _head_from_session(record)
            await writeJsonFile(sessionPath(session_id), repaired)
    elif available_count > committed_count:
        reason = "body_ahead"
    elif body["truncatedTail"]:
        reason = "body_tail_ignored"
    return {"record": record, "changed": changed, "reason": reason}


def hydrateSessionHeadFromBodyBytes(
    sessionId: str, head: dict[str, Any], bodyBytes: bytes
) -> dict[str, Any]:
    """Hydrate source bytes already captured by a bounded recovery scanner."""
    session_id = parseSessionId(sessionId)
    _validate_v2_head(head, session_id)
    body = _scan_body_bytes(bodyBytes, session_id)
    count = min(head["messageCount"], len(body["messages"]))
    return {
        "record": _hydrate_head(head, body["messages"][:count]),
        "bodyShorter": len(body["messages"]) < head["messageCount"],
        "bodyAhead": len(body["messages"]) > head["messageCount"],
        "truncatedTail": body["truncatedTail"],
    }


async def commitSessionV2(
    sessionId: str,
    previousRaw: dict[str, Any] | None,
    previousSession: dict[str, Any] | None,
    nextSession: dict[str, Any],
    *,
    faultInjector: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    safe = _sanitize_media(copy.deepcopy(nextSession))
    if safe.get("id") != session_id or not isinstance(safe.get("messages"), list):
        raise ValueError(f"Invalid hydrated session for v2 commit: {session_id}.")
    old_messages = (
        previousSession.get("messages", [])
        if isinstance(previousSession, dict) and isinstance(previousSession.get("messages"), list)
        else []
    )
    new_messages = safe["messages"]
    entries = _body_delta(session_id, old_messages, new_messages, isSessionHeadV2(previousRaw))
    await _inject(faultInjector, "before_body")
    if entries:
        await _run_storage_io(_append_body_sync, sessionMessagesPath(session_id), entries)
    await _inject(faultInjector, "after_body")
    await writeJsonFile(sessionPath(session_id), _head_from_session(safe))
    await _inject(faultInjector, "after_head")
    return safe


async def migrateSessionToV2(
    sessionId: str,
    *,
    faultInjector: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    async with withRecordTransaction("session", session_id):
        raw = await readJsonFile(sessionPath(session_id), None)
        hydrated = await hydratePersistedSession(session_id, raw)
        record = hydrated["record"]
        if record is None:
            raise FileNotFoundError(f"Session not found: {session_id}")
        if isSessionHeadV2(raw):
            return {"sessionId": session_id, "changed": hydrated["changed"], "schemaVersion": 2}
        await commitSessionV2(
            session_id,
            raw,
            record,
            record,
            faultInjector=faultInjector,
        )
        return {"sessionId": session_id, "changed": True, "schemaVersion": 2}


async def writeBackSessionV1(sessionId: str) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    async with withRecordTransaction("session", session_id):
        raw = await readJsonFile(sessionPath(session_id), None)
        hydrated = await hydratePersistedSession(session_id, raw)
        record = hydrated["record"]
        if record is None:
            raise FileNotFoundError(f"Session not found: {session_id}")
        if not isSessionHeadV2(raw):
            return {"sessionId": session_id, "changed": False, "schemaVersion": 1}
        await writeJsonFile(sessionPath(session_id), record)
        return {
            "sessionId": session_id,
            "changed": True,
            "schemaVersion": 1,
            "messageCount": len(record["messages"]),
        }


async def compactSessionMessageBody(sessionId: str) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    async with withRecordTransaction("session", session_id):
        raw = await readJsonFile(sessionPath(session_id), None)
        hydrated = await hydratePersistedSession(session_id, raw)
        record = hydrated["record"]
        if record is None:
            raise FileNotFoundError(f"Session not found: {session_id}")
        if not isSessionHeadV2(raw):
            raise ValueError("Session body compaction requires schema v2.")
        entries = [_message_entry(session_id, index, message) for index, message in enumerate(record["messages"])]
        byte_count = await _run_storage_io(_replace_body_sync, sessionMessagesPath(session_id), entries)
        return {"sessionId": session_id, "messageCount": len(entries), "bytes": byte_count}


def _validate_v2_head(head: dict[str, Any], session_id: str) -> None:
    body_ref = head.get("bodyRef")
    expected_name = f"{session_id}.messages.jsonl"
    valid = (
        isinstance(head.get("messageCount"), int)
        and not isinstance(head.get("messageCount"), bool)
        and head["messageCount"] >= 0
        and isinstance(body_ref, dict)
        and body_ref.get("format") == SESSION_BODY_FORMAT
        and body_ref.get("path") == expected_name
        and isinstance(body_ref.get("messageKeyIndex"), int)
        and body_ref["messageKeyIndex"] >= 0
        and isinstance(head.get("hasPersistableContent"), bool)
        and isinstance(head.get("firstUserText"), str)
        and "messages" not in head
    )
    if not valid:
        raise ValueError(f"Malformed session schema v2 head: {session_id}.")


def _head_from_session(session: dict[str, Any]) -> dict[str, Any]:
    session_id = parseSessionId(session["id"])
    keys = [key for key in session if key not in _HEAD_FIELDS]
    message_index = keys.index("messages") if "messages" in keys else len(keys)
    head = {key: copy.deepcopy(session[key]) for key in keys if key != "messages"}
    messages = session.get("messages") if isinstance(session.get("messages"), list) else []
    head.update(
        {
            "schemaVersion": SESSION_SCHEMA_V2,
            "messageCount": len(messages),
            "bodyRef": {
                "path": f"{session_id}.messages.jsonl",
                "format": SESSION_BODY_FORMAT,
                "messageKeyIndex": message_index,
            },
            "hasPersistableContent": _has_persistable_messages(messages),
            "firstUserText": _first_user_text(messages),
        }
    )
    return head


def _hydrate_head(head: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    index = head["bodyRef"]["messageKeyIndex"]
    items = [(key, copy.deepcopy(value)) for key, value in head.items() if key not in _HEAD_FIELDS]
    index = min(index, len(items))
    items.insert(index, ("messages", copy.deepcopy(messages)))
    return dict(items)


def _has_persistable_messages(messages: list[Any]) -> bool:
    for message in messages:
        if not isinstance(message, dict) or not _is_genuine_user_request(message):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list) and any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and str(block.get("text") or "").strip()
            for block in content
        ):
            return True
    return False


def _first_user_text(messages: list[Any]) -> str:
    for message in messages:
        if not isinstance(message, dict) or not _is_genuine_user_request(message):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:512]
        if isinstance(content, list):
            value = " ".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if value:
                return value[:512]
    return ""


def _body_delta(
    session_id: str,
    old_messages: list[Any],
    new_messages: list[Any],
    previous_is_v2: bool,
) -> list[dict[str, Any]]:
    common = 0
    while common < min(len(old_messages), len(new_messages)) and old_messages[common] == new_messages[common]:
        common += 1
    entries: list[dict[str, Any]] = []
    if not previous_is_v2:
        entries.append({"type": "truncate", "session_id": session_id, "message_count": 0})
        common = 0
    elif common < len(old_messages):
        entries.append({"type": "truncate", "session_id": session_id, "message_count": common})
    entries.extend(_message_entry(session_id, index, new_messages[index]) for index in range(common, len(new_messages)))
    return entries


def _message_entry(session_id: str, index: int, message: Any) -> dict[str, Any]:
    return {"type": "message", "session_id": session_id, "message_index": index, "message": message}


def _read_body_sync(path: str, session_id: str) -> dict[str, Any]:
    return _scan_body_sync(path, session_id)


def _scan_body_sync(path: str, session_id: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return {"messages": [], "truncatedTail": False, "validBytes": 0}
    return _scan_body_bytes(raw, session_id)


def _scan_body_bytes(raw: bytes, session_id: str) -> dict[str, Any]:
    if not raw:
        return {"messages": [], "truncatedTail": False, "validBytes": 0}
    complete_end = len(raw) if raw.endswith(b"\n") else raw.rfind(b"\n") + 1
    complete = raw[:complete_end]
    try:
        payload = complete.rstrip(b"\n")
        entries = json.loads(b"[" + payload.replace(b"\n", b",") + b"]") if payload else []
        messages = _apply_body_entries(entries, session_id)
    except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _scan_body_fallback(raw, session_id)
    return {
        "messages": messages,
        "truncatedTail": complete_end < len(raw),
        "validBytes": complete_end,
    }


def _apply_body_entries(entries: Any, session_id: str) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise ValueError("Session body is not a JSONL record sequence.")
    messages: list[dict[str, Any]] = []
    for entry in entries:
        messages = _apply_body_entry(messages, entry, session_id)
    return messages


def _apply_body_entry(
    messages: list[dict[str, Any]], entry: Any, session_id: str
) -> list[dict[str, Any]]:
    if not isinstance(entry, dict) or entry.get("session_id") != session_id:
        raise ValueError("Session body identity mismatch.")
    if entry.get("type") == "truncate":
        count = entry.get("message_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > len(messages):
            raise ValueError("Invalid session body truncate marker.")
        del messages[count:]
        return messages
    index = entry.get("message_index")
    message = entry.get("message")
    if entry.get("type") != "message" or not isinstance(index, int) or index < 0 or not isinstance(message, dict):
        raise ValueError("Invalid session body message record.")
    if index > len(messages):
        raise ValueError("Non-contiguous session body message index.")
    del messages[index:]
    messages.append(message)
    return messages


def _scan_body_fallback(raw: bytes, session_id: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    valid_bytes = 0
    offset = 0
    for raw_line in raw.splitlines(keepends=True):
        offset += len(raw_line)
        if not raw_line.strip():
            valid_bytes = offset
            continue
        try:
            entry = json.loads(raw_line.decode("utf-8"))
            messages = _apply_body_entry(messages, entry, session_id)
        except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            break
        valid_bytes = offset
    return {
        "messages": messages,
        "truncatedTail": valid_bytes < len(raw),
        "validBytes": valid_bytes,
    }


def _append_body_sync(path: str, entries: list[dict[str, Any]]) -> None:
    started = time.perf_counter_ns() if _persistence_observers_active() else 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    encoded = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries).encode("utf-8")
    scan = _scan_body_sync(path, str(entries[0]["session_id"]))
    mode = "r+b" if os.path.exists(path) else "w+b"
    with open(path, mode) as handle:
        handle.truncate(scan["validBytes"])
        handle.seek(0, os.SEEK_END)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    if started:
        _record_persistence_io("append", path, len(encoded), time.perf_counter_ns() - started)


def _replace_body_sync(path: str, entries: list[dict[str, Any]]) -> int:
    started = time.perf_counter_ns() if _persistence_observers_active() else 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    encoded = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries).encode("utf-8")
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    if started:
        _record_persistence_io("write", path, len(encoded), time.perf_counter_ns() - started)
    return len(encoded)


async def _inject(injector: Callable[[str], Any] | None, boundary: str) -> None:
    if injector is None:
        return
    result = injector(boundary)
    if hasattr(result, "__await__"):
        await result
