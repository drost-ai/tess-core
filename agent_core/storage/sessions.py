from __future__ import annotations

import copy
import time
from ..payloads.media_persistence import sanitizeMediaForPersistence as _sanitize_media_for_persistence
from ..contracts.records import SessionRecord
from ..session.message_origin import isGenuineUserRequest as _is_genuine_user_request
from .persistence_metrics import (
    persistenceObserversActive as _persistence_observers_active,
    recordPersistenceIo as _record_persistence_io,
)
from ..runtime.identifiers import RuntimeIdentifierError
from ..session.message_history import (
    createSessionMessageList as _create_session_message_list,
    setSessionPersistenceCheckpoint as _set_session_persistence_checkpoint,
)
from ..providers.usage_costs import createUsageTotals
from .io_executor import runStorageIo as _run_storage_io
import json
import os
import uuid
from typing import Any

from .._utils import now_iso
from .json_io import _in_process_lock, readJsonFile, readJsonFileTolerant, withRecordTransaction, writeJsonFile
from .paths import (
    compactionCheckpointPath,
    compactionManifestPath,
    getStateRoot,
    sessionPath,
    sessionTranscriptLogPath,
)
from .session_schema_v2 import commitSessionV2, hydratePersistedSession, isSessionHeadV2
from .session_transactions import normalizeSessionRevision

async def saveSession(session: SessionRecord) -> None:
    path = sessionPath(session["id"])
    async with withRecordTransaction("session", session["id"]):
        raw = await readJsonFileTolerant(path, None)
        hydrated = await hydratePersistedSession(session["id"], raw)
        current = hydrated["record"]
        merged = _sanitize_media_for_persistence(_merge_session_for_full_save(current, session))
        if isinstance(current, dict) and ("revision" in current or "revision" in merged):
            merged["revision"] = max(
                normalizeSessionRevision(current.get("revision")),
                normalizeSessionRevision(merged.get("revision")),
            )
        if current != merged or not isSessionHeadV2(raw):
            commit = await commitSessionV2(session["id"], raw, current, merged, presanitized=True)
            committed = commit["session"]
            _set_session_persistence_checkpoint(
                session,
                normalizeSessionRevision(committed.get("revision")),
                len(session.get("messages") or []),
                commit["bodyCommitId"],
            )
async def appendSessionTranscriptLogEntries(sessionId: str, entries: list[Any]) -> str:
    path = sessionTranscriptLogPath(sessionId)
    # The former synchronous body also serialized same-process callers by
    # blocking the event loop. Preserve that ordering explicitly now that the
    # append runs on a worker.
    async with _in_process_lock(f"{path}#transcript-append"):
        await _run_storage_io(_append_session_transcript_log_entries_sync, path, entries)
    return path


def _append_session_transcript_log_entries_sync(path: str, entries: list[Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if entries:
        measure = _persistence_observers_active()
        started_ns = time.perf_counter_ns() if measure else 0
        previous_size = os.path.getsize(path) if measure and os.path.exists(path) else 0
        with open(path, "a", encoding="utf-8") as handle:
            for entry in entries:
                json.dump(_sanitize_media_for_persistence(entry), handle, ensure_ascii=False)
                handle.write("\n")
        if measure:
            _record_persistence_io(
                "append",
                path,
                os.path.getsize(path) - previous_size,
                time.perf_counter_ns() - started_ns,
            )

_ADDITIVE_SESSION_METADATA_LISTS = {
    "backgroundSessionIds",
    "childAgentIds",
    "childSessionIds",
    "monitorIds",
    "subagentLaunchGroups",
    "taskIds",
}


def _merge_session_for_full_save(current: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Preserve cross-process metadata when a caller saves a full session.

    The active session owner remains authoritative for messages, todos, prompt,
    permission mode, and other top-level fields. Background subsystems mutate a
    small set of metadata fields independently; stale full-session snapshots
    must not erase those append-only or monotonic updates.
    """
    if not isinstance(current, dict) or current.get("id") != incoming.get("id"):
        return incoming
    merged = {**current, **incoming}
    current_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
    incoming_metadata = incoming.get("metadata") if isinstance(incoming.get("metadata"), dict) else {}
    metadata = {**current_metadata, **incoming_metadata}
    for key in _ADDITIVE_SESSION_METADATA_LISTS:
        if key in current_metadata or key in incoming_metadata:
            metadata[key] = _dedupe_list([*(incoming_metadata.get(key) or []), *(current_metadata.get(key) or [])])
    if "usage" in current_metadata or "usage" in incoming_metadata:
        metadata["usage"] = _merge_numeric_record(current_metadata.get("usage"), incoming_metadata.get("usage"))
    if "compactions" in current_metadata or "compactions" in incoming_metadata:
        metadata["compactions"] = _max_number(current_metadata.get("compactions"), incoming_metadata.get("compactions"))
    if "fullTranscriptLog" in current_metadata or "fullTranscriptLog" in incoming_metadata:
        metadata["fullTranscriptLog"] = _choose_full_transcript_log(
            current_metadata.get("fullTranscriptLog"),
            incoming_metadata.get("fullTranscriptLog"),
        )
    if "runtimeRestore" in current_metadata or "runtimeRestore" in incoming_metadata:
        metadata["runtimeRestore"] = _choose_newer_saved_at(
            current_metadata.get("runtimeRestore"),
            incoming_metadata.get("runtimeRestore"),
        )
    if "compactionState" in current_metadata or "compactionState" in incoming_metadata:
        metadata["compactionState"] = _choose_newer_compaction_state(
            current_metadata.get("compactionState"),
            incoming_metadata.get("compactionState"),
        )
    if "providerFailureCount" in current_metadata or "providerFailureCount" in incoming_metadata:
        metadata["providerFailureCount"] = _max_number(
            current_metadata.get("providerFailureCount"),
            incoming_metadata.get("providerFailureCount"),
        )
    if "subagentLaunchGroupRecords" in current_metadata or "subagentLaunchGroupRecords" in incoming_metadata:
        metadata["subagentLaunchGroupRecords"] = _merge_launch_group_records(
            current_metadata.get("subagentLaunchGroupRecords"),
            incoming_metadata.get("subagentLaunchGroupRecords"),
        )
    merged["metadata"] = metadata
    return merged


def _dedupe_list(values: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _merge_numeric_record(current: Any, incoming: Any) -> dict[str, Any]:
    if not isinstance(current, dict):
        return dict(incoming) if isinstance(incoming, dict) else {}
    if not isinstance(incoming, dict):
        return dict(current)
    merged = {**current, **incoming}
    for key in set(current) | set(incoming):
        if isinstance(current.get(key), (int, float)) and isinstance(incoming.get(key), (int, float)):
            merged[key] = max(current[key], incoming[key])
    return merged


def _merge_launch_group_records(current: Any, incoming: Any) -> dict[str, Any]:
    current_records = current if isinstance(current, dict) else {}
    incoming_records = incoming if isinstance(incoming, dict) else {}
    merged: dict[str, Any] = {**current_records, **incoming_records}
    for group_id in set(current_records) | set(incoming_records):
        current_group = current_records.get(group_id) if isinstance(current_records.get(group_id), dict) else {}
        incoming_group = incoming_records.get(group_id) if isinstance(incoming_records.get(group_id), dict) else {}
        group = {**current_group, **incoming_group}
        group["childAgentIds"] = _dedupe_list([*(incoming_group.get("childAgentIds") or []), *(current_group.get("childAgentIds") or [])])
        group["taskIds"] = _dedupe_list([*(incoming_group.get("taskIds") or []), *(current_group.get("taskIds") or [])])
        merged[group_id] = group
    return merged


def _max_number(current: Any, incoming: Any) -> Any:
    if isinstance(current, (int, float)) and isinstance(incoming, (int, float)):
        return max(current, incoming)
    return incoming if incoming is not None else current


def _choose_full_transcript_log(current: Any, incoming: Any) -> Any:
    if not isinstance(current, dict):
        return incoming
    if not isinstance(incoming, dict):
        return current
    current_count = current.get("archivedMessageCount")
    incoming_count = incoming.get("archivedMessageCount")
    if isinstance(current_count, int) and isinstance(incoming_count, int) and current_count > incoming_count:
        return current
    return incoming


def _choose_newer_saved_at(current: Any, incoming: Any) -> Any:
    if not isinstance(current, dict):
        return incoming
    if not isinstance(incoming, dict):
        return current
    return current if str(current.get("savedAt") or "") > str(incoming.get("savedAt") or "") else incoming


def _choose_newer_compaction_state(current: Any, incoming: Any) -> Any:
    if not isinstance(current, dict):
        return incoming
    if not isinstance(incoming, dict):
        return current
    current_generation = current.get("latestCompactionGeneration")
    incoming_generation = incoming.get("latestCompactionGeneration")
    if isinstance(current_generation, int) and isinstance(incoming_generation, int):
        return current if current_generation > incoming_generation else incoming
    return current if str(current.get("latestCompactionAt") or "") > str(incoming.get("latestCompactionAt") or "") else incoming

MAX_SHALLOW_SESSION_BYTES = 16 * 1024 * 1024

async def loadSessionRecordShallow(sessionId: str) -> SessionRecord | None:
    """Read only the bounded session JSON record; never open its archive."""

    return await _run_storage_io(_load_session_record_shallow_sync, sessionPath(sessionId), sessionId)


def _load_session_record_shallow_sync(path: str, session_id: str) -> SessionRecord | None:
    try:
        stat_result = os.stat(path)
        if stat_result.st_size > MAX_SHALLOW_SESSION_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not _valid_shallow_session(record, session_id):
        return None
    return record


def _valid_shallow_session(value: Any, expected_id: str) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("id") == expected_id
        and isinstance(value.get("cwd"), str)
        and isinstance(value.get("createdAt"), str)
        and isinstance(value.get("updatedAt"), str)
        and isinstance(value.get("metadata"), dict)
        and (
            isinstance(value.get("messages"), list)
            or (
                isSessionHeadV2(value)
                and isinstance(value.get("messageCount"), int)
                and isinstance(value.get("bodyRef"), dict)
                and isinstance(value.get("hasPersistableContent"), bool)
                and isinstance(value.get("firstUserText"), str)
            )
        )
    )

async def loadCompactionManifest(sessionId: str) -> dict[str, Any] | None:
    return await readJsonFile(compactionManifestPath(sessionId), None)

async def saveCompactionManifest(manifest: dict[str, Any]) -> None:
    await writeJsonFile(
        compactionManifestPath(manifest["sessionId"]),
        _sanitize_media_for_persistence(manifest),
    )

async def saveCompactionCheckpoint(checkpoint: dict[str, Any]) -> None:
    await writeJsonFile(
        compactionCheckpointPath(
            checkpoint["sessionId"], int(checkpoint["generation"])
        ),
        _sanitize_media_for_persistence(checkpoint),
    )

def hasPersistableSessionMessages(session: SessionRecord) -> bool:
    if isSessionHeadV2(session) and isinstance(session.get("hasPersistableContent"), bool):
        return session["hasPersistableContent"]
    for message in session.get("messages", []):
        if not _is_genuine_user_request(message):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    return True
    return False

async def listSessions() -> list[SessionRecord]:
    directory = os.path.join(getStateRoot(), "sessions")
    names = await _run_storage_io(_list_json_names_sync, directory)
    records: list[dict[str, Any]] = []
    for name in names:
        try:
            record = await loadSessionRecordShallow(name[:-5])
        except RuntimeIdentifierError:
            continue
        if record and hasPersistableSessionMessages(record):
            records.append(record)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)


def createSessionListCache() -> dict[str, Any]:
    """Create an instance-owned summary cache keyed by path/mtime/size."""

    entries: dict[str, dict[str, Any]] = {}
    stats = {"scanned": 0, "parsed": 0, "cacheHits": 0, "invalid": 0}

    async def list_cached() -> list[dict[str, Any]]:
        directory = os.path.join(getStateRoot(), "sessions")
        scanned = await _run_storage_io(_scan_session_directory_sync, directory)
        seen: set[str] = set()
        summaries: list[dict[str, Any]] = []
        stats["scanned"] += len(scanned)
        for name, path, fingerprint in scanned:
            seen.add(path)
            cached = entries.get(path)
            if cached and cached.get("fingerprint") == fingerprint:
                stats["cacheHits"] += 1
                if cached.get("summary"):
                    summaries.append(copy.deepcopy(cached["summary"]))
                continue
            session_id = name[:-5]
            stats["parsed"] += 1
            try:
                record = await loadSessionRecordShallow(session_id)
            except RuntimeIdentifierError:
                record = None
            summary = _session_list_summary(record) if record and hasPersistableSessionMessages(record) else None
            if record is None:
                stats["invalid"] += 1
            entries[path] = {"fingerprint": fingerprint, "summary": summary}
            if summary:
                summaries.append(copy.deepcopy(summary))
        for path in list(entries):
            if path not in seen:
                entries.pop(path, None)
        return sorted(summaries, key=lambda item: item.get("updatedAt", ""), reverse=True)

    def get_stats() -> dict[str, int]:
        return {**stats, "entries": len(entries)}

    def invalidate(session_id: str | None = None) -> None:
        if session_id is None:
            entries.clear()
            return
        entries.pop(os.path.realpath(sessionPath(session_id)), None)

    return {"list": list_cached, "getStats": get_stats, "invalidate": invalidate}


def _list_json_names_sync(directory: str) -> list[str]:
    try:
        return [name for name in os.listdir(directory) if name.endswith(".json")]
    except FileNotFoundError:
        return []


def _scan_session_directory_sync(directory: str) -> list[tuple[str, str, tuple[int, int]]]:
    scanned: list[tuple[str, str, tuple[int, int]]] = []
    for name in _list_json_names_sync(directory):
        path = os.path.realpath(os.path.join(directory, name))
        try:
            file_stat = os.stat(path)
        except OSError:
            continue
        scanned.append((name, path, (file_stat.st_mtime_ns, file_stat.st_size)))
    return scanned


def _session_list_summary(record: SessionRecord) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    summary_metadata = {
        key: copy.deepcopy(metadata[key])
        for key in (
            "originalCwd",
            "gitRoot",
            "workspaceName",
            "prNumber",
            "prUrl",
            "taskIds",
            "monitorIds",
            "childAgentIds",
            "childSessionIds",
            "backgroundSessionIds",
            "remoteSessionId",
            "bridgeId",
        )
        if key in metadata
    }
    return {
        "id": record.get("id"),
        "cwd": record.get("cwd"),
        "name": record.get("name"),
        "createdAt": record.get("createdAt"),
        "updatedAt": record.get("updatedAt"),
        "provider": record.get("provider"),
        "model": record.get("model"),
        "permissionMode": record.get("permissionMode"),
        "metadata": summary_metadata,
        "firstUserText": record.get("firstUserText"),
        "hasPersistableContent": True,
    }

def createEmptySession(params: dict[str, Any] | None = None, **kwargs: Any) -> SessionRecord:
    params = {**(params or {}), **kwargs}
    timestamp = now_iso()
    return {
        "id": params.get("sessionId") or str(uuid.uuid4()),
        "cwd": params["cwd"],
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "name": params.get("name"),
        "provider": params["provider"],
        "providerProfileName": params.get("providerProfileName"),
        "model": params["model"],
        "effort": params.get("effort"),
        "systemPrompt": params["systemPrompt"],
        "permissionMode": params["permissionMode"],
        "messages": _create_session_message_list(),
        "todos": [],
        "metadata": {
            "originalCwd": params.get("originalCwd") or params["cwd"],
            "assistantName": params.get("assistantName"),
            "assistantDescription": params.get("assistantDescription"),
            "usage": createUsageTotals(),
            "compactions": 0,
            "depth": 0,
            "childSessionIds": [],
            "childAgentIds": [],
        },
    }
