from __future__ import annotations

from ...storage import loadBridgeDeliveryLedger

from ..._utils import now_iso
from ...storage import (
    listRemoteSessions,
    loadSessionRecordShallow,
    updateSessionRecord,
    withRecordTransaction,
)
from ...storage.remote import (
    loadBridge,
    loadRemoteSession,
    saveBridge,
    saveRemoteSession,
)


async def isCurrentBridgeGeneration(bridge):
    current = await loadBridge(bridge["id"])
    session = await loadSessionRecordShallow(bridge["sessionId"])
    remote = await loadRemoteSession(bridge["remoteSessionId"]) if bridge.get("remoteSessionId") else None
    return bool(
        current and current.get("schemaVersion") == 2 and current.get("status") == "active"
        and current.get("generation") == bridge.get("generation") and session
        and session.get("metadata", {}).get("bridgeId") == bridge["id"]
        and session.get("metadata", {}).get("bridgeGeneration") == bridge.get("generation")
        and (not bridge.get("remoteSessionId") or (remote and _remote_owns(remote, bridge)))
    )


async def closeCurrentBridgeGeneration(bridge, eventName="bridge.closed"):
    async with withRecordTransaction("bridge-session", bridge["sessionId"]):
        current = await loadBridge(bridge["id"])
        if not current or current.get("generation") != bridge.get("generation"):
            return
        session = await loadSessionRecordShallow(bridge["sessionId"])
        metadata = (session or {}).get("metadata", {})
        owns = metadata.get("bridgeId") == bridge["id"] and metadata.get("bridgeGeneration") == bridge.get("generation")
        if current.get("status") == "active" and not owns:
            await saveBridge({
                **current, "status": "superseded", "revision": int(current.get("revision") or 0) + 1,
                "updatedAt": now_iso(), "lastEventName": "bridge.superseded",
            })
            return
        if current.get("status") not in {"active", "closed", "failed"}:
            return
        timestamp = now_iso()
        if current.get("status") == "active":
            await saveBridge({
                **current, "status": "closed", "revision": int(current.get("revision") or 0) + 1,
                "updatedAt": timestamp, "lastEventName": eventName,
            })
        if owns:
            def clear_pointer(latest):
                next_session = {**latest, "metadata": {**latest["metadata"]}, "updatedAt": timestamp}
                next_session["metadata"].pop("bridgeId", None)
                next_session["metadata"].pop("bridgeGeneration", None)
                return {"session": next_session, "changed": next_session != latest}
            await updateSessionRecord(bridge["sessionId"], clear_pointer)
        for remote in await listRemoteSessions():
            if not _remote_owns(remote, bridge):
                continue
            remote.pop("bridgeId", None)
            remote.pop("bridgeGeneration", None)
            remote.update({"bridgeStatus": "closed", "updatedAt": timestamp})
            if not _terminal_remote(remote.get("status")):
                remote.update({
                    "status": "idle", "lastEventName": eventName,
                    "lastEventAt": timestamp, "lastEventSummary": "Bridge closed",
                })
            await saveRemoteSession(remote)


def _remote_owns(remote, bridge):
    return remote.get("sessionId") == bridge["sessionId"] and remote.get("bridgeId") == bridge["id"] and remote.get("bridgeGeneration") == bridge.get("generation")


def _terminal_remote(status):
    return status in {"completed", "cancelled", "failed", "closed"}


__all__ = ["closeCurrentBridgeGeneration", "isCurrentBridgeGeneration"]


async def requireBridgeLedger(bridge):
    ledger = await loadBridgeDeliveryLedger(bridge["id"])
    if not ledger or ledger.get("sessionId") != bridge["sessionId"] or ledger.get("generation") != bridge.get("generation"):
        raise RuntimeError("Active bridge delivery ledger is missing or generation-mismatched.")
    return ledger
