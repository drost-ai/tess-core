from __future__ import annotations

from .shared import *

def sessionPath(sessionId: str) -> str:
    return os.path.join(getStateRoot(), "sessions", f"{sessionId}.json")

def sessionArchiveDir(sessionId: str) -> str:
    return os.path.join(getStateRoot(), "sessions", sessionId)

def sessionTranscriptLogPath(sessionId: str) -> str:
    return os.path.join(sessionArchiveDir(sessionId), "full-transcript-with-tools.jsonl")

async def saveSession(session: dict[str, Any]) -> None:
    await writeJsonFile(sessionPath(session["id"]), session)

async def appendSessionTranscriptLogEntries(sessionId: str, entries: list[Any]) -> str:
    path = sessionTranscriptLogPath(sessionId)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if entries:
        with open(path, "a", encoding="utf-8") as handle:
            for entry in entries:
                json.dump(entry, handle, ensure_ascii=False)
                handle.write("\n")
    return path

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

async def loadSession(sessionId: str) -> dict[str, Any] | None:
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

def hasPersistableSessionMessages(session: dict[str, Any]) -> bool:
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

async def listSessions() -> list[dict[str, Any]]:
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

def createEmptySession(params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
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
