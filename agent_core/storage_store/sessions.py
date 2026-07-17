from __future__ import annotations

import copy

from ..contracts.records import SessionRecord

from .shared import *

def sessionPath(sessionId: str) -> str:
    return os.path.join(getStateRoot(), "sessions", f"{sessionId}.json")

def sessionArchiveDir(sessionId: str) -> str:
    return os.path.join(getStateRoot(), "sessions", sessionId)

def sessionTranscriptLogPath(sessionId: str) -> str:
    return os.path.join(sessionArchiveDir(sessionId), "full-transcript-with-tools.jsonl")

async def saveSession(session: SessionRecord) -> None:
    path = sessionPath(session["id"])
    async with fileLock(path):
        current = await readJsonFile(path, None)
        await writeJsonFile(path, _redact_persisted_image_payloads(_merge_session_for_full_save(current, session)))


async def mutateSessionRecord(sessionId: str, mutate) -> SessionRecord | None:
    """Locked read-modify-write for cross-process session metadata updates.

    Reconciles the archived transcript while holding the same record lock,
    applies ``mutate`` (return the record, or ``None`` after mutating in
    place), and persists with the same redaction as ``saveSession``. Returns
    ``None`` without writing when the session does not exist.
    """
    path = sessionPath(sessionId)
    async with fileLock(path):
        session = await readJsonFile(path, None)
        if session is None:
            return None
        reconciliation = await _reconcile_session_record(sessionId, session)
        session = reconciliation["record"]
        updated = mutate(session)
        if updated is None:
            updated = session
        await writeJsonFile(path, _redact_persisted_image_payloads(updated))
        return updated

async def appendSessionTranscriptLogEntries(sessionId: str, entries: list[Any]) -> str:
    path = sessionTranscriptLogPath(sessionId)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if entries:
        with open(path, "a", encoding="utf-8") as handle:
            for entry in entries:
                json.dump(_redact_persisted_image_payloads(entry), handle, ensure_ascii=False)
                handle.write("\n")
    return path

def _redact_persisted_image_payloads(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_persisted_image_payloads(item) for item in value]
    if not isinstance(value, dict):
        return value
    block_type = str(value.get("type") or "").strip().lower()
    is_image = block_type in {"image", "input_image", "image_url"}
    out: dict[str, Any] = {}
    for key, item in value.items():
        cleaned = str(key or "").strip()
        if is_image and cleaned in {"data", "data_base64", "base64", "image_url", "url"}:
            out[cleaned] = _persisted_image_placeholder(value)
            continue
        out[cleaned] = _redact_persisted_image_payloads(item)
    return out

def _persisted_image_placeholder(value: dict[str, Any]) -> str:
    mime_type = str(value.get("mime_type") or value.get("mimeType") or value.get("mime") or "image").strip() or "image"
    path = (
        str(
            value.get("path")
            or value.get("file_path")
            or value.get("filePath")
            or value.get("source_path")
            or ""
        ).strip()
    )
    suffix = f":{path}" if path else ""
    return f"<image:{mime_type}{suffix}:current-turn-only>"


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
    async with fileLock(path):
        record = await readJsonFile(path, None)
        if not record:
            return {"record": None, "changed": False, "reason": None}
        result = await _reconcile_session_record(sessionId, record)
        if result["changed"]:
            await writeJsonFile(path, _redact_persisted_image_payloads(result["record"]))
        return result


async def loadSession(sessionId: str) -> SessionRecord | None:
    result = await loadSessionWithReconciliation(sessionId)
    return result["record"]


async def _reconcile_session_record(session_id: str, record: SessionRecord) -> dict[str, Any]:
    transcript_path = sessionTranscriptLogPath(session_id)
    try:
        with open(transcript_path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
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
    metadata_mismatch = any(
        previous_log.get(key) != value for key, value in next_log.items()
    )
    updated_at_behind = bool(
        latest and str(record.get("updatedAt") or "") < latest
    )
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


MAX_SHALLOW_SESSION_BYTES = 16 * 1024 * 1024

async def loadSessionRecordShallow(sessionId: str) -> SessionRecord | None:
    """Read only the bounded session JSON record; never open its archive."""

    path = sessionPath(sessionId)
    try:
        stat_result = os.stat(path)
        if stat_result.st_size > MAX_SHALLOW_SESSION_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not _valid_shallow_session(record, sessionId):
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
        and isinstance(value.get("messages"), list)
    )

async def loadCompactionManifest(sessionId: str) -> dict[str, Any] | None:
    return await readJsonFile(compactionManifestPath(sessionId), None)

async def saveCompactionManifest(manifest: dict[str, Any]) -> None:
    await writeJsonFile(compactionManifestPath(manifest["sessionId"]), manifest)

async def saveCompactionCheckpoint(checkpoint: dict[str, Any]) -> None:
    await writeJsonFile(compactionCheckpointPath(checkpoint["sessionId"], int(checkpoint["generation"])), checkpoint)

def hasPersistableSessionMessages(session: SessionRecord) -> bool:
    for message in session.get("messages", []):
        if message.get("role") != "user":
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
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".json")]
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for name in names:
        record = await loadSessionRecordShallow(name[:-5])
        if record and hasPersistableSessionMessages(record):
            records.append(record)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)


def createSessionListCache() -> dict[str, Any]:
    """Create an instance-owned summary cache keyed by path/mtime/size."""

    entries: dict[str, dict[str, Any]] = {}
    stats = {"scanned": 0, "parsed": 0, "cacheHits": 0, "invalid": 0}

    async def list_cached() -> list[dict[str, Any]]:
        directory = os.path.join(getStateRoot(), "sessions")
        try:
            names = [name for name in os.listdir(directory) if name.endswith(".json")]
        except FileNotFoundError:
            names = []
        seen: set[str] = set()
        summaries: list[dict[str, Any]] = []
        stats["scanned"] += len(names)
        for name in names:
            path = os.path.realpath(os.path.join(directory, name))
            try:
                file_stat = os.stat(path)
            except OSError:
                continue
            seen.add(path)
            fingerprint = (file_stat.st_mtime_ns, file_stat.st_size)
            cached = entries.get(path)
            if cached and cached.get("fingerprint") == fingerprint:
                stats["cacheHits"] += 1
                if cached.get("summary"):
                    summaries.append(copy.deepcopy(cached["summary"]))
                continue
            session_id = name[:-5]
            stats["parsed"] += 1
            record = await loadSessionRecordShallow(session_id)
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
        "messages": [],
        "todos": [],
        "metadata": {
            "originalCwd": params.get("originalCwd") or params["cwd"],
            "assistantName": params.get("assistantName"),
            "assistantDescription": params.get("assistantDescription"),
            "usage": {
                "turns": 0,
                "toolCalls": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheCreationInputTokens": 0,
                "cacheReadInputTokens": 0,
            },
            "compactions": 0,
            "depth": 0,
            "childSessionIds": [],
            "childAgentIds": [],
            "planMode": {"state": "inactive"},
        },
    }
