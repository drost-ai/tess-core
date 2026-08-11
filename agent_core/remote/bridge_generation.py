from __future__ import annotations

from ..storage import load_bridge_delivery_ledger

from .._utils import now_iso
from ..storage import (
    list_remote_sessions,
    load_session_record_shallow,
    mutate_session_record_transaction,
    with_record_transaction,
)
from ..storage.remote import (
    load_bridge,
    load_remote_session,
    save_bridge,
    save_remote_session,
)


async def isCurrentBridgeGeneration(bridge):
    current = await load_bridge(bridge["id"])
    session = await load_session_record_shallow(bridge["sessionId"])
    remote = await load_remote_session(bridge["remoteSessionId"]) if bridge.get("remoteSessionId") else None
    return bool(
        current and current.get("schemaVersion") == 2 and current.get("status") == "active"
        and current.get("generation") == bridge.get("generation") and session
        and session.get("metadata", {}).get("bridgeId") == bridge["id"]
        and session.get("metadata", {}).get("bridgeGeneration") == bridge.get("generation")
        and (not bridge.get("remoteSessionId") or (remote and _remote_owns(remote, bridge)))
    )


async def closeCurrentBridgeGeneration(bridge, eventName="bridge.closed"):
    async with with_record_transaction("bridge-session", bridge["sessionId"]):
        current = await load_bridge(bridge["id"])
        if not current or current.get("generation") != bridge.get("generation"):
            return
        session = await load_session_record_shallow(bridge["sessionId"])
        metadata = (session or {}).get("metadata", {})
        owns = metadata.get("bridgeId") == bridge["id"] and metadata.get("bridgeGeneration") == bridge.get("generation")
        if current.get("status") == "active" and not owns:
            await save_bridge({
                **current, "status": "superseded", "revision": int(current.get("revision") or 0) + 1,
                "updatedAt": now_iso(), "lastEventName": "bridge.superseded",
            })
            return
        if current.get("status") not in {"active", "closed", "failed"}:
            return
        timestamp = now_iso()
        if current.get("status") == "active":
            await save_bridge({
                **current, "status": "closed", "revision": int(current.get("revision") or 0) + 1,
                "updatedAt": timestamp, "lastEventName": eventName,
            })
        if owns:
            def clear_pointer(latest):
                next_session = {**latest, "metadata": {**latest["metadata"]}, "updatedAt": timestamp}
                next_session["metadata"].pop("bridgeId", None)
                next_session["metadata"].pop("bridgeGeneration", None)
                return {"session": next_session, "changed": next_session != latest}
            await mutate_session_record_transaction(bridge["sessionId"], clear_pointer, requireExisting=True)
        for remote in await list_remote_sessions():
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
            await save_remote_session(remote)


def _remote_owns(remote, bridge):
    return remote.get("sessionId") == bridge["sessionId"] and remote.get("bridgeId") == bridge["id"] and remote.get("bridgeGeneration") == bridge.get("generation")


def _terminal_remote(status):
    return status in {"completed", "cancelled", "failed", "closed"}


__all__ = ["closeCurrentBridgeGeneration", "isCurrentBridgeGeneration"]


async def requireBridgeLedger(bridge):
    ledger = await load_bridge_delivery_ledger(bridge["id"])
    if not ledger or ledger.get("sessionId") != bridge["sessionId"] or ledger.get("generation") != bridge.get("generation"):
        raise RuntimeError("Active bridge delivery ledger is missing or generation-mismatched.")
    return ledger
