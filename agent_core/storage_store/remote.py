from __future__ import annotations

from .shared import *
from ..runtime_identifiers import RuntimeIdentifierError, parseBridgeId, parseRemoteSessionId

def remoteSessionPath(remoteSessionId: str) -> str:
    return os.path.join(
        getStateRoot(),
        "remote-sessions",
        f"{parseRemoteSessionId(remoteSessionId)}.json",
    )

async def saveRemoteSession(record: dict[str, Any]) -> None:
    await writeJsonFile(remoteSessionPath(record["id"]), _omit_none_fields(record))

async def loadRemoteSession(remoteSessionId: str) -> dict[str, Any] | None:
    return await readJsonFile(remoteSessionPath(remoteSessionId), None)

async def listRemoteSessions() -> list[dict[str, Any]]:
    records = await _list_json_records(os.path.join(getStateRoot(), "remote-sessions"), loadRemoteSession)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)

def bridgePath(bridgeId: str) -> str:
    return os.path.join(getStateRoot(), "bridges", f"{parseBridgeId(bridgeId)}.json")

def bridgeInboxPath(bridgeId: str) -> str:
    return os.path.join(getStateRoot(), "bridges", f"{parseBridgeId(bridgeId)}.inbox.json")

def bridgeEventsPath(bridgeId: str) -> str:
    return os.path.join(getStateRoot(), "bridges", f"{parseBridgeId(bridgeId)}.events.json")

async def saveBridge(record: dict[str, Any]) -> None:
    await writeJsonFile(bridgePath(record["id"]), _omit_none_fields(record))

async def loadBridge(bridgeId: str) -> dict[str, Any] | None:
    return await readJsonFile(bridgePath(bridgeId), None)

async def listBridges() -> list[dict[str, Any]]:
    directory = os.path.join(getStateRoot(), "bridges")
    try:
        names = [
            name for name in os.listdir(directory)
            if name.endswith(".json") and not name.endswith(".inbox.json") and not name.endswith(".events.json")
        ]
    except FileNotFoundError:
        return []
    records = []
    for name in names:
        try:
            record = await loadBridge(name[:-5])
        except RuntimeIdentifierError:
            continue
        if record:
            records.append(record)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)

async def loadBridgeInbox(bridgeId: str) -> list[dict[str, Any]]:
    return await readJsonFile(bridgeInboxPath(bridgeId), [])

async def appendBridgeInboxMessage(message: dict[str, Any]) -> None:
    existing = await loadBridgeInbox(message["bridgeId"])
    await writeJsonFile(bridgeInboxPath(message["bridgeId"]), _omit_none_fields([*existing, message]))

async def loadBridgeEvents(bridgeId: str) -> list[dict[str, Any]]:
    return await readJsonFile(bridgeEventsPath(bridgeId), [])

async def appendBridgeEvent(record: dict[str, Any]) -> None:
    existing = await loadBridgeEvents(record["bridgeId"])
    await writeJsonFile(bridgeEventsPath(record["bridgeId"]), _omit_none_fields([*existing, record]))
