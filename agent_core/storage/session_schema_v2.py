from __future__ import annotations

import copy
import os
import uuid
from typing import Any, Callable

from ..payloads.media_persistence import sanitizeMediaForPersistence as _sanitize_media
from ..session.message_origin import isGenuineUserRequest as _is_genuine_user_request
from ..runtime.identifiers import parseSessionId
from ..session.message_history import (
    createSessionMessageList as _create_session_message_list,
    setSessionPersistenceCheckpoint as _set_session_persistence_checkpoint,
)
from .io_executor import runStorageIo as _run_storage_io
from .session_body_v2 import (
    _append_body_sync,
    _body_commit_id,
    _commit_entry,
    _message_entry,
    _read_body_sync,
    _replace_body_sync,
    _scan_body_bytes,
)
from .json_io import readJsonFileTolerant, withRecordTransaction, writeJsonFile
from .paths import getStateRoot, sessionPath

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
        return {"record": None, "changed": False, "reason": None, "bodyCommitId": None}
    if not isinstance(raw, dict) or raw.get("id") != session_id:
        raise ValueError(f"Session record identity mismatch for {session_id}.")
    assertSessionSchemaSupported(raw)
    if not isSessionHeadV2(raw):
        if not isinstance(raw.get("messages"), list):
            raise ValueError(f"Legacy session {session_id} has no message list.")
        return {
            "record": copy.deepcopy(raw),
            "changed": False,
            "reason": None,
            "bodyCommitId": None,
        }
    _validate_v2_head(raw, session_id)
    body_commit_id = _body_commit_id(raw)
    body = await _run_storage_io(
        _read_body_sync,
        sessionMessagesPath(session_id),
        session_id,
        body_commit_id,
    )
    committed_count = raw["messageCount"]
    available_count = len(body["messages"])
    commit_missing = bool(body_commit_id and not body["commitFound"])
    if commit_missing and available_count >= committed_count:
        raise ValueError(f"Session body commit marker is missing: {session_id}.")
    selected_count = min(committed_count, available_count)
    record = _hydrate_head(raw, body["messages"][:selected_count])
    reason = None
    changed = False
    if available_count < committed_count:
        reason = "body_shorter"
        changed = True
        if repairHead:
            if commit_missing:
                body_commit_id = str(uuid.uuid4())
                await _run_storage_io(
                    _append_body_sync,
                    sessionMessagesPath(session_id),
                    [_commit_entry(session_id, body_commit_id, available_count)],
                    None,
                )
            repaired = _head_from_session(
                record,
                body_commit_id=body_commit_id,
            )
            await writeJsonFile(sessionPath(session_id), repaired)
    elif body["bodyAhead"] or available_count > committed_count:
        reason = "body_ahead"
    elif body["truncatedTail"]:
        reason = "body_tail_ignored"
    _set_session_persistence_checkpoint(
        record,
        _normalize_revision(record.get("revision")),
        len(record.get("messages") or []),
        body_commit_id,
    )
    return {
        "record": record,
        "changed": changed,
        "reason": reason,
        "bodyCommitId": body_commit_id,
    }


def hydrateSessionHeadFromBodyBytes(
    sessionId: str, head: dict[str, Any], bodyBytes: bytes
) -> dict[str, Any]:
    """Hydrate source bytes already captured by a bounded recovery scanner."""
    session_id = parseSessionId(sessionId)
    _validate_v2_head(head, session_id)
    body_commit_id = _body_commit_id(head)
    body = _scan_body_bytes(bodyBytes, session_id, body_commit_id)
    if (
        body_commit_id
        and not body["commitFound"]
        and len(body["messages"]) >= head["messageCount"]
    ):
        raise ValueError(f"Session body commit marker is missing: {session_id}.")
    count = min(head["messageCount"], len(body["messages"]))
    record = _hydrate_head(head, body["messages"][:count])
    _set_session_persistence_checkpoint(
        record,
        _normalize_revision(record.get("revision")),
        len(record.get("messages") or []),
        body_commit_id,
    )
    return {
        "record": record,
        "bodyShorter": len(body["messages"]) < head["messageCount"],
        "bodyAhead": body["bodyAhead"] or len(body["messages"]) > head["messageCount"],
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
    previous_commit_id = _body_commit_id(previousRaw)
    body_commit_id = previous_commit_id
    if entries or body_commit_id is None:
        body_commit_id = str(uuid.uuid4())
        entries.append(_commit_entry(session_id, body_commit_id, len(new_messages)))
    await _inject(faultInjector, "before_body")
    if entries:
        await _run_storage_io(
            _append_body_sync,
            sessionMessagesPath(session_id),
            entries,
            previous_commit_id,
        )
    await _inject(faultInjector, "after_body")
    head = _head_from_session(safe, body_commit_id=body_commit_id)
    await writeJsonFile(sessionPath(session_id), head)
    await _inject(faultInjector, "after_head")
    return {"session": safe, "persistedHead": head, "bodyCommitId": body_commit_id}


async def commitSessionV2Delta(
    sessionId: str,
    previousRaw: dict[str, Any],
    nextHead: dict[str, Any],
    messages: list[Any],
    *,
    faultInjector: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Commit an authority-checked append suffix without hydrating its prefix."""

    session_id = parseSessionId(sessionId)
    _validate_v2_head(previousRaw, session_id)
    previous_commit_id = _body_commit_id(previousRaw)
    if previous_commit_id is None:
        raise ValueError("A v2 delta commit requires a persisted body commit marker.")
    base_count = previousRaw["messageCount"]
    if len(messages) < base_count:
        raise ValueError("A session delta cannot truncate its persisted message prefix.")
    safe_head = _sanitize_media(
        {key: value for key, value in nextHead.items() if key not in _HEAD_FIELDS and key != "messages"}
    )
    if not isinstance(safe_head, dict) or safe_head.get("id") != session_id:
        raise ValueError(f"Invalid live session head for v2 delta commit: {session_id}.")
    safe_suffix = _sanitize_media(messages[base_count:])
    if not isinstance(safe_suffix, list):
        raise TypeError("Sanitized session delta must remain a message list.")
    entries = [
        _message_entry(session_id, base_count + offset, message)
        for offset, message in enumerate(safe_suffix)
    ]
    body_commit_id = previous_commit_id
    if entries:
        body_commit_id = str(uuid.uuid4())
        entries.append(_commit_entry(session_id, body_commit_id, len(messages)))
    await _inject(faultInjector, "before_body")
    if entries:
        await _run_storage_io(
            _append_body_sync,
            sessionMessagesPath(session_id),
            entries,
            previous_commit_id,
        )
    await _inject(faultInjector, "after_body")
    head = _head_from_delta(
        previousRaw,
        safe_head,
        safe_suffix,
        len(messages),
        body_commit_id,
    )
    await writeJsonFile(sessionPath(session_id), head)
    await _inject(faultInjector, "after_head")
    return {
        "head": safe_head,
        "persistedHead": head,
        "appendedMessages": safe_suffix,
        "bodyCommitId": body_commit_id,
    }


def sessionLiveHeadFromV2(sessionId: str, raw: dict[str, Any]) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    _validate_v2_head(raw, session_id)
    return {
        key: copy.deepcopy(value)
        for key, value in raw.items()
        if key not in _HEAD_FIELDS
    }


async def migrateSessionToV2(
    sessionId: str,
    *,
    faultInjector: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    async with withRecordTransaction("session", session_id):
        raw = await readJsonFileTolerant(sessionPath(session_id), None)
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
        raw = await readJsonFileTolerant(sessionPath(session_id), None)
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
        raw = await readJsonFileTolerant(sessionPath(session_id), None)
        hydrated = await hydratePersistedSession(session_id, raw)
        record = hydrated["record"]
        if record is None:
            raise FileNotFoundError(f"Session not found: {session_id}")
        if not isSessionHeadV2(raw):
            raise ValueError("Session body compaction requires schema v2.")
        body_commit_id = _body_commit_id(raw) or str(uuid.uuid4())
        entries = [_message_entry(session_id, index, message) for index, message in enumerate(record["messages"])]
        entries.append(
            _commit_entry(session_id, body_commit_id, len(record["messages"]))
        )
        byte_count = await _run_storage_io(_replace_body_sync, sessionMessagesPath(session_id), entries)
        if _body_commit_id(raw) is None:
            repaired_head = copy.deepcopy(raw)
            repaired_head["bodyRef"]["commitId"] = body_commit_id
            await writeJsonFile(sessionPath(session_id), repaired_head)
        return {
            "sessionId": session_id,
            "messageCount": len(record["messages"]),
            "bytes": byte_count,
        }


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
        and (
            body_ref.get("commitId") is None
            or isinstance(body_ref.get("commitId"), str)
        )
        and isinstance(head.get("hasPersistableContent"), bool)
        and isinstance(head.get("firstUserText"), str)
        and "messages" not in head
    )
    if not valid:
        raise ValueError(f"Malformed session schema v2 head: {session_id}.")


def _head_from_session(
    session: dict[str, Any], *, body_commit_id: str | None = None
) -> dict[str, Any]:
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
                **({"commitId": body_commit_id} if body_commit_id else {}),
            },
            "hasPersistableContent": _has_persistable_messages(messages),
            "firstUserText": _first_user_text(messages),
        }
    )
    return head


def _head_from_delta(
    previous: dict[str, Any],
    live_head: dict[str, Any],
    safe_suffix: list[Any],
    message_count: int,
    body_commit_id: str,
) -> dict[str, Any]:
    body_ref = copy.deepcopy(previous["bodyRef"])
    body_ref["commitId"] = body_commit_id
    first_user_text = str(previous.get("firstUserText") or "")
    if not first_user_text:
        first_user_text = _first_user_text(safe_suffix)
    head = copy.deepcopy(live_head)
    head.update(
        {
            "schemaVersion": SESSION_SCHEMA_V2,
            "messageCount": message_count,
            "bodyRef": body_ref,
            "hasPersistableContent": bool(
                previous.get("hasPersistableContent")
                or _has_persistable_messages(safe_suffix)
            ),
            "firstUserText": first_user_text,
        }
    )
    return head


def _hydrate_head(head: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    index = head["bodyRef"]["messageKeyIndex"]
    items = [(key, copy.deepcopy(value)) for key, value in head.items() if key not in _HEAD_FIELDS]
    index = min(index, len(items))
    items.insert(index, ("messages", _create_session_message_list(messages)))
    return dict(items)


def _normalize_revision(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


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


async def _inject(injector: Callable[[str], Any] | None, boundary: str) -> None:
    if injector is None:
        return
    result = injector(boundary)
    if hasattr(result, "__await__"):
        await result
