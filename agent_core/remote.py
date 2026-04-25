from __future__ import annotations

import uuid
from typing import Any

from ._utils import now_iso
from .storage import (
    appendBridgeEvent,
    appendBridgeInboxMessage,
    loadBridgeEvents,
    loadBridgeInbox,
    listBridges,
    listRemoteSessions,
    saveBridge,
    saveRemoteSession,
)


async def createRemoteSession(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    now = now_iso()
    record = {"id": args.get("id") or str(uuid.uuid4()), "sessionId": args["sessionId"], "title": args.get("title") or "Remote Session", "createdAt": now, "updatedAt": now, "status": "created", "cwd": args.get("cwd"), "originalCwd": args.get("originalCwd") or args.get("cwd"), "channelServers": args.get("channelServers", [])}
    await saveRemoteSession(record)
    return record


async def updateRemoteSession(remoteSessionId: str, updates: dict[str, Any]) -> dict[str, Any]:
    sessions = await listRemoteSessions()
    record = next((item for item in sessions if item.get("id") == remoteSessionId), {"id": remoteSessionId})
    record.update(updates, updatedAt=now_iso())
    await saveRemoteSession(record)
    return record


async def resolveTeleportSession(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
    sessions = await listRemoteSessions()
    return sessions[0] if sessions else None


async def createBridgeRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    bridge = {"id": args.get("id") or str(uuid.uuid4()), "sessionId": args.get("sessionId"), "createdAt": now_iso(), "updatedAt": now_iso(), "status": "active", "cwd": args.get("cwd")}
    await saveBridge(bridge)
    return {"bridge": bridge, "send": lambda text: sendBridgeMessage({"bridgeId": bridge["id"], "text": text})}


async def sendBridgeMessage(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    message = {"id": str(uuid.uuid4()), "bridgeId": args["bridgeId"], "createdAt": now_iso(), "source": args.get("source", "local"), "text": args["text"]}
    await appendBridgeInboxMessage(message)
    await appendBridgeEvent({"id": str(uuid.uuid4()), "bridgeId": args["bridgeId"], "createdAt": now_iso(), "event": "message", "data": message})
    return message


async def formatBridgeTail(bridgeId: str, limit: int = 20) -> str:
    inbox = await loadBridgeInbox(bridgeId)
    return "\n".join(f"{item.get('createdAt')} {item.get('source')}: {item.get('text')}" for item in inbox[-limit:])


async def listActiveBridgeRows() -> list[str]:
    return [f"{bridge.get('id')} {bridge.get('status')} {bridge.get('cwd', '')}" for bridge in await listBridges() if bridge.get("status") in ("active", "idle")]


def findMatchingRemoteSessions(sessions: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = query.lower()
    return [session for session in sessions if q in session.get("id", "").lower() or q in session.get("title", "").lower()]


def formatRemoteSessionCandidate(remote: dict[str, Any]) -> str:
    return f"{remote.get('id', '')[:8]}  {remote.get('title', ''):<24}  {remote.get('status', ''):<12}  {remote.get('updatedAt', '')[:19]}"
