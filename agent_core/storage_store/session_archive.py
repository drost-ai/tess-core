from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .._media_persistence import sanitizeMediaForPersistence as _sanitize_media_for_persistence
from ..contracts.records import SessionRecord
from ..session_message_history import (
    setSessionPersistenceCheckpoint as _set_session_persistence_checkpoint,
)
from .io_executor import runStorageIo as _run_storage_io
from .shared import *


def _parse_archived_messages(raw: str) -> list[dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") == "message" and isinstance(entry.get("message_index"), int):
            role = entry.get("role")
            if role in ("user", "assistant") and "content" in entry:
                indexed[entry["message_index"]] = {"role": role, "content": entry["content"]}
        elif entry.get("type") == "repair" and isinstance(
            entry.get("truncated_to_message_index"), int
        ):
            cutoff = max(0, entry["truncated_to_message_index"])
            indexed = {index: message for index, message in indexed.items() if index < cutoff}
    return [indexed[index] for index in sorted(indexed)]


def _latest_archived_timestamp(raw: str) -> str | None:
    latest = None
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry.get("persisted_at"), str):
            latest = entry["persisted_at"]
    return latest


def parseLatestArchivedTimestamp(raw: str) -> str | None:
    return _latest_archived_timestamp(raw)


async def loadSessionWithReconciliation(sessionId: str) -> dict[str, Any]:
    """Full selected-session load with an explicit, convergent repair verdict."""
    path = sessionPath(sessionId)
    async with withRecordTransaction("session", sessionId):
        raw = await readJsonFile(path, None)
        if not raw:
            recovered = await _rebuild_session_from_transcript_archive(sessionId)
            if recovered is None:
                return {"record": None, "changed": False, "reason": None}
            safe_recovered = _sanitize_media_for_persistence(recovered)
            commit = await commitSessionV2(sessionId, None, None, safe_recovered)
            _set_session_persistence_checkpoint(
                safe_recovered,
                normalizeSessionRevision(safe_recovered.get("revision")),
                len(safe_recovered.get("messages") or []),
                commit["bodyCommitId"],
            )
            return {"record": safe_recovered, "changed": True, "reason": "archive_recovery"}
        hydration = await hydratePersistedSession(sessionId, raw)
        record = hydration["record"]
        result = await _reconcile_session_record(sessionId, record)
        safe_record = _sanitize_media_for_persistence(result["record"])
        body_commit_id = hydration.get("bodyCommitId")
        if result["changed"]:
            commit = await commitSessionV2(sessionId, raw, record, safe_record)
            body_commit_id = commit["bodyCommitId"]
        _set_session_persistence_checkpoint(
            safe_record,
            normalizeSessionRevision(safe_record.get("revision")),
            len(safe_record.get("messages") or []),
            body_commit_id,
        )
        changed = bool(result["changed"] or hydration["changed"])
        reason = result["reason"] or hydration["reason"]
        return {**result, "record": safe_record, "changed": changed, "reason": reason}


async def loadSession(sessionId: str) -> SessionRecord | None:
    result = await loadSessionWithReconciliation(sessionId)
    return result["record"]


async def _reconcile_session_record(session_id: str, record: SessionRecord) -> dict[str, Any]:
    transcript_path = sessionTranscriptLogPath(session_id)
    raw = await _run_storage_io(_read_text_sync, transcript_path, False)
    if raw is None:
        return {"record": record, "changed": False, "reason": None}
    archived = _parse_archived_messages(raw)
    latest = _latest_archived_timestamp(raw)
    messages = record.get("messages") if isinstance(record.get("messages"), list) else []
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    previous_log = metadata.get("fullTranscriptLog") if isinstance(metadata.get("fullTranscriptLog"), dict) else {}
    next_log = {
        "path": transcript_path,
        "archivedMessageCount": len(archived),
        "lastArchivedAt": latest or previous_log.get("lastArchivedAt") or record.get("updatedAt"),
    }
    archive_ahead = len(archived) > len(messages)
    metadata_mismatch = any(previous_log.get(key) != value for key, value in next_log.items())
    updated_at_behind = bool(latest and str(record.get("updatedAt") or "") < latest)
    if not archive_ahead and not metadata_mismatch and not updated_at_behind:
        return {"record": record, "changed": False, "reason": None}
    repaired = copy.deepcopy(record)
    if archive_ahead:
        repaired["messages"] = archived
    repaired.setdefault("metadata", {})["fullTranscriptLog"] = next_log
    if updated_at_behind:
        repaired["updatedAt"] = latest
    return {
        "record": repaired,
        "changed": True,
        "reason": "archive_ahead" if archive_ahead else "metadata_mismatch",
    }


async def _rebuild_session_from_transcript_archive(session_id: str) -> SessionRecord | None:
    transcript_path = sessionTranscriptLogPath(session_id)
    raw = await _run_storage_io(_read_text_sync, transcript_path, True)
    if raw is None:
        return None
    messages = _parse_archived_messages(raw)
    if not messages:
        return None
    header = _parse_archived_session_header(raw, session_id)
    if header is None:
        return None
    latest = _latest_archived_timestamp(raw)
    header["messages"] = messages
    header.setdefault("metadata", {})["fullTranscriptLog"] = {
        "path": transcript_path,
        "archivedMessageCount": len(messages),
        "lastArchivedAt": latest or header.get("updatedAt"),
    }
    return header


def _read_text_sync(path: str, suppress_os_errors: bool) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        if suppress_os_errors:
            return None
        raise


def _parse_archived_session_header(raw: str, session_id: str) -> SessionRecord | None:
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        candidate = entry.get("session") if isinstance(entry, dict) else None
        required = ("cwd", "createdAt", "updatedAt", "provider", "model", "systemPrompt", "permissionMode")
        if not (
            entry.get("type") == "session_header"
            and entry.get("schema_version") == 1
            and entry.get("session_id") == session_id
            and isinstance(candidate, dict)
            and candidate.get("id") == session_id
            and all(isinstance(candidate.get(key), str) and candidate.get(key) for key in required)
            and isinstance(candidate.get("metadata"), dict)
        ):
            continue
        recovered = createEmptySession(
            cwd=candidate["cwd"],
            provider=candidate["provider"],
            providerProfileName=candidate.get("providerProfileName"),
            model=candidate["model"],
            effort=candidate.get("effort"),
            systemPrompt=candidate["systemPrompt"],
            permissionMode=candidate["permissionMode"],
            name=candidate.get("name"),
            assistantName=candidate["metadata"].get("assistantName"),
            assistantDescription=candidate["metadata"].get("assistantDescription"),
            sessionId=session_id,
            originalCwd=candidate["metadata"].get("originalCwd") or candidate["cwd"],
        )
        recovered["createdAt"] = candidate["createdAt"]
        recovered["updatedAt"] = candidate["updatedAt"]
        recovered["todos"] = copy.deepcopy(candidate.get("todos") or [])
        recovered["metadata"] = {**recovered["metadata"], **copy.deepcopy(candidate["metadata"])}
        recovered["metadata"].pop("fullTranscriptLog", None)
        return recovered
    return None
