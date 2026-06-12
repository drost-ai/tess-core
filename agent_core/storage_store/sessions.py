from __future__ import annotations

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

    Loads through ``loadSession`` (so archived-transcript merging matches the
    unlocked path), applies ``mutate`` (return the record, or ``None`` after
    mutating in place), and persists with the same redaction as
    ``saveSession``. Returns ``None`` without writing when the session does
    not exist.
    """
    path = sessionPath(sessionId)
    async with fileLock(path):
        session = await loadSession(sessionId)
        if session is None:
            return None
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

async def loadSession(sessionId: str) -> SessionRecord | None:
    path = sessionPath(sessionId)
    record = await readJsonFile(path, None)
    if not record:
        return None
    transcript_path = sessionTranscriptLogPath(sessionId)
    try:
        with open(transcript_path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return record
    archived = _parse_archived_messages(raw)
    latest = _latest_archived_timestamp(raw)
    if len(archived) > len(record.get("messages", [])):
        record["messages"] = archived
    metadata = record.setdefault("metadata", {})
    metadata["fullTranscriptLog"] = {
        "path": transcript_path,
        "archivedMessageCount": max(len(archived), len(record.get("messages", []))),
        "lastArchivedAt": latest or metadata.get("fullTranscriptLog", {}).get("lastArchivedAt") or record.get("updatedAt"),
    }
    return record

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
        record = await loadSession(name[:-5])
        if record and hasPersistableSessionMessages(record):
            records.append(record)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)

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
