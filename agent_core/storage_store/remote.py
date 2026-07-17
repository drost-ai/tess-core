from __future__ import annotations

from .shared import *
from ..persisted_record_scanner import readPersistedJsonRecord, scanPersistedJsonRecords
from ..persisted_record_validation import isBridgeRecord, isRemoteSessionRecord
from ..runtime_identifiers import (
    RuntimeIdentifierError,
    parseBridgeId,
    parseRemoteSessionId,
    tryParseBridgeId,
    tryParseRemoteSessionId,
)

BRIDGE_LOG_MAX_ENTRIES = 4_096


class BridgeStorageError(RuntimeError):
    code = "bridge.storage_unavailable"

    def __init__(self, message: str, issue: dict[str, Any] | None = None):
        super().__init__(message)
        self.issue = issue

def remoteSessionPath(remoteSessionId: str) -> str:
    return os.path.join(
        getStateRoot(),
        "remote-sessions",
        f"{parseRemoteSessionId(remoteSessionId)}.json",
    )

async def saveRemoteSession(record: dict[str, Any]) -> None:
    await writeJsonFile(remoteSessionPath(record["id"]), _omit_none_fields(record))

async def loadRemoteSession(remoteSessionId: str) -> dict[str, Any] | None:
    remote_id = parseRemoteSessionId(remoteSessionId)
    result = await readPersistedJsonRecord(
        family="remote_session",
        id=remote_id,
        filePath=remoteSessionPath(remote_id),
        validate=isRemoteSessionRecord,
    )
    return result.get("record")

async def listRemoteSessions() -> list[dict[str, Any]]:
    directory = os.path.join(getStateRoot(), "remote-sessions")
    scan = await scanPersistedJsonRecords(
        family="remote_session",
        directory=directory,
        select=lambda entry: _remote_json_candidate(directory, entry, tryParseRemoteSessionId),
        validate=isRemoteSessionRecord,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)

def bridgePath(bridgeId: str) -> str:
    return os.path.join(getStateRoot(), "bridges", f"{parseBridgeId(bridgeId)}.json")

def bridgeInboxPath(bridgeId: str) -> str:
    return os.path.join(getStateRoot(), "bridges", f"{parseBridgeId(bridgeId)}.inbox.json")

def bridgeEventsPath(bridgeId: str) -> str:
    return os.path.join(getStateRoot(), "bridges", f"{parseBridgeId(bridgeId)}.events.json")

async def saveBridge(record: dict[str, Any]) -> None:
    await writeJsonFile(bridgePath(record["id"]), _omit_none_fields(record))

async def loadBridge(bridgeId: str) -> dict[str, Any] | None:
    bridge_id = parseBridgeId(bridgeId)
    result = await readPersistedJsonRecord(
        family="bridge",
        id=bridge_id,
        filePath=bridgePath(bridge_id),
        validate=isBridgeRecord,
    )
    return result.get("record")

async def listBridges() -> list[dict[str, Any]]:
    directory = os.path.join(getStateRoot(), "bridges")
    def select(entry: os.DirEntry[str]) -> dict[str, str] | None:
        if not entry.name.endswith(".json") or entry.name.endswith(
            (".inbox.json", ".events.json", ".ledger.json")
        ):
            return None
        return _remote_json_candidate(directory, entry, tryParseBridgeId)

    scan = await scanPersistedJsonRecords(
        family="bridge",
        directory=directory,
        select=select,
        validate=isBridgeRecord,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)

async def loadBridgeInbox(bridgeId: str) -> list[dict[str, Any]]:
    bridge_id = parseBridgeId(bridgeId)
    return await _load_bridge_array(
        family="bridge_inbox",
        bridge_id=bridge_id,
        file_path=bridgeInboxPath(bridge_id),
        validate_entry=lambda value: _is_bridge_inbox_message(value, bridge_id),
    )

async def appendBridgeInboxMessage(message: dict[str, Any]) -> None:
    bridge_id = parseBridgeId(message["bridgeId"])
    async with withRecordTransaction("bridge-coordination", bridge_id):
        existing = await loadBridgeInbox(bridge_id)
        result = _append_unique_coordination_record(existing, message)
        if result["changed"]:
            await writeJsonFile(bridgeInboxPath(bridge_id), result["value"])

async def loadBridgeEvents(bridgeId: str) -> list[dict[str, Any]]:
    bridge_id = parseBridgeId(bridgeId)
    return await _load_bridge_array(
        family="bridge_event_log",
        bridge_id=bridge_id,
        file_path=bridgeEventsPath(bridge_id),
        validate_entry=lambda value: _is_bridge_event_record(value, bridge_id),
    )

async def appendBridgeEvent(record: dict[str, Any]) -> None:
    bridge_id = parseBridgeId(record["bridgeId"])
    async with withRecordTransaction("bridge-coordination", bridge_id):
        existing = await loadBridgeEvents(bridge_id)
        result = _append_unique_coordination_record(existing, record)
        if result["changed"]:
            await writeJsonFile(bridgeEventsPath(bridge_id), result["value"])


def _append_unique_coordination_record(existing: Any, record: dict[str, Any]) -> dict[str, Any]:
    records = existing if isinstance(existing, list) else []
    record_id = record.get("id")
    if record_id and any(isinstance(item, dict) and item.get("id") == record_id for item in records):
        return {"value": records, "changed": False}
    return {"value": _omit_none_fields([*records, record]), "changed": True}


async def _load_bridge_array(
    *,
    family: str,
    bridge_id: str,
    file_path: str,
    validate_entry,
) -> list[dict[str, Any]]:
    result = await readPersistedJsonRecord(
        family=family,
        id=bridge_id,
        filePath=file_path,
        validate=lambda value, _candidate: (
            isinstance(value, list)
            and len(value) <= BRIDGE_LOG_MAX_ENTRIES
            and all(validate_entry(item) for item in value)
        ),
    )
    issue = result.get("issue")
    if issue:
        raise BridgeStorageError(f"{family} {bridge_id} is unavailable: {issue['reason']}.", issue)
    return result.get("record") or []


def _remote_json_candidate(directory: str, entry: os.DirEntry[str], parse_id) -> dict[str, str] | None:
    if not entry.name.endswith(".json"):
        return None
    record_id = parse_id(entry.name[:-5])
    if record_id is None:
        return None
    return {"id": record_id, "filePath": os.path.join(directory, entry.name)}


def _is_bridge_inbox_message(value: Any, bridge_id: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("bridgeId") == bridge_id
        and isinstance(value.get("id"), str)
        and bool(value["id"])
        and isinstance(value.get("createdAt"), str)
        and value.get("source") in {"bridge", "remote", "local"}
        and isinstance(value.get("text"), str)
    )


def _is_bridge_event_record(value: Any, bridge_id: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("bridgeId") == bridge_id
        and isinstance(value.get("id"), str)
        and bool(value["id"])
        and isinstance(value.get("createdAt"), str)
        and isinstance(value.get("event"), str)
        and bool(value["event"])
        and isinstance(value.get("data"), dict)
    )
