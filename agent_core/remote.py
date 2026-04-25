from __future__ import annotations

import uuid
from typing import Any

from ._utils import now_iso
from .storage import (
    appendBridgeEvent,
    appendBridgeInboxMessage,
    loadBridge,
    loadBridgeEvents,
    loadBridgeInbox,
    loadRemoteSession,
    listBridges,
    listRemoteSessions,
    saveBridge,
    saveRemoteSession,
)


async def createRemoteSession(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args.get("session") or {}
    now = now_iso()
    metadata = session.get("metadata", {}) if isinstance(session, dict) else {}
    record = {"id": args.get("id") or str(uuid.uuid4()), "sessionId": args.get("sessionId") or session.get("id"), "title": args.get("title") or args.get("description") or session.get("name") or metadata.get("workspaceName") or "Remote Session", "description": args.get("description"), "createdAt": now, "updatedAt": now, "status": "created", "cwd": args.get("cwd") or session.get("cwd"), "originalCwd": args.get("originalCwd") or metadata.get("originalCwd") or args.get("cwd") or session.get("cwd"), "workspaceName": metadata.get("workspaceName"), "gitRoot": metadata.get("gitRoot"), "gitBranch": metadata.get("gitBranch"), "prNumber": metadata.get("prNumber"), "prUrl": metadata.get("prUrl"), "channelServers": args.get("channelServers", []), "unreadBridgeMessages": 0, "foregroundSessionId": args.get("sessionId") or session.get("id")}
    await saveRemoteSession(record)
    return record


async def updateRemoteSession(remoteSessionId: str, updates: dict[str, Any]) -> dict[str, Any]:
    record = await loadRemoteSession(remoteSessionId)
    if not record:
        return {"id": remoteSessionId, **updates, "updatedAt": now_iso()}
    record.update(updates, updatedAt=now_iso())
    await saveRemoteSession(record)
    return record


async def resolveTeleportSession(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
    sessions = await listRemoteSessions()
    return sessions[0] if sessions else None


async def createBridgeRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args.get("session") or {}
    emit = args.get("emit") or (lambda *_: None)
    now = now_iso()
    bridge = {"id": args.get("id") or str(uuid.uuid4()), "sessionId": args.get("sessionId") or session.get("id"), "remoteSessionId": args.get("remoteSessionId"), "name": args.get("name"), "createdAt": now, "updatedAt": now, "status": "active", "cwd": args.get("cwd") or session.get("cwd"), "lastEventName": "bridge.started", "pendingMessages": 0, "messageCount": 0}
    await saveBridge(bridge)
    if bridge.get("remoteSessionId"):
        await updateRemoteSession(bridge["remoteSessionId"], {"bridgeId": bridge["id"], "status": "active", "lastEventAt": now, "lastEventSummary": "Bridge attached"})

    async def record_event(event: str, data: dict[str, Any] | None = None) -> None:
        timestamp = now_iso()
        await appendBridgeEvent({"id": str(uuid.uuid4()), "bridgeId": bridge["id"], "createdAt": timestamp, "event": event, "data": data or {}})
        current = await loadBridge(bridge["id"])
        if current:
            current.update({"updatedAt": timestamp, "lastEventName": event})
            await saveBridge(current)
        if bridge.get("remoteSessionId"):
            await updateRemoteSession(bridge["remoteSessionId"], {"status": "idle" if event == "bridge.closed" else "completed" if event == "session.completed" else "active", "bridgeStatus": "closed" if event == "bridge.closed" else "active", "lastEventName": event, "lastEventAt": timestamp, "lastEventSummary": _truncate_remote_summary(_build_bridge_event_summary(event, data or {}))})

    async def close() -> None:
        timestamp = now_iso()
        current = await loadBridge(bridge["id"])
        if current:
            current.update({"status": "closed", "updatedAt": timestamp, "lastEventName": "bridge.closed"})
            await saveBridge(current)
        await record_event("bridge.closed", {"bridge_id": bridge["id"]})

    await record_event("bridge.started", {"bridge_id": bridge["id"], "session_id": bridge.get("sessionId"), "remote_session_id": bridge.get("remoteSessionId"), "name": bridge.get("name")})
    emit("bridge.started", {"bridge_id": bridge["id"], "session_id": bridge.get("sessionId"), "remote_session_id": bridge.get("remoteSessionId"), "name": bridge.get("name")})
    return {"bridge": bridge, "send": lambda text: sendBridgeMessage({"bridgeId": bridge["id"], "text": text}), "recordEvent": record_event, "close": close}


async def sendBridgeMessage(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    bridge = await loadBridge(args["bridgeId"])
    if not bridge:
        raise RuntimeError(f"Bridge not found: {args['bridgeId']}")
    if bridge.get("status") != "active":
        raise RuntimeError(f"Bridge is not active: {args['bridgeId']}")
    message = {"id": str(uuid.uuid4()), "bridgeId": args["bridgeId"], "createdAt": now_iso(), "source": args.get("source", "bridge"), "text": args["text"]}
    await appendBridgeInboxMessage(message)
    current = await loadBridge(args["bridgeId"])
    if current:
        current.update({"updatedAt": message["createdAt"], "lastEventName": "bridge.message.queued", "lastMessageAt": message["createdAt"], "lastMessageSource": message["source"], "pendingMessages": int(current.get("pendingMessages") or 0) + 1})
        await saveBridge(current)
    await appendBridgeEvent({"id": str(uuid.uuid4()), "bridgeId": args["bridgeId"], "createdAt": message["createdAt"], "event": "bridge.message.queued", "data": {"message_id": message["id"], "text": message["text"], "source": message["source"]}})
    if bridge.get("remoteSessionId"):
        remote = await loadRemoteSession(bridge["remoteSessionId"])
        await updateRemoteSession(bridge["remoteSessionId"], {"status": "awaiting_input", "bridgeStatus": "active", "unreadBridgeMessages": int((remote or {}).get("unreadBridgeMessages") or 0) + 1, "lastEventName": "bridge.message.queued", "lastEventAt": message["createdAt"], "lastEventSummary": _truncate_remote_summary(f"Queued bridge input: {message['text']}")})
    return message


async def formatBridgeTail(bridgeId: str, limit: int = 20) -> str:
    events = await loadBridgeEvents(bridgeId)
    if not events:
        return "No bridge events recorded."
    return "\n".join(f"{item.get('createdAt')}  {item.get('event')}  {_truncate_remote_summary(str((item.get('data') or {}).get('text') or (item.get('data') or {}).get('summary') or item.get('data')))}" for item in events[-limit:])


async def listActiveBridgeRows() -> list[str]:
    return [f"{bridge.get('id')} {bridge.get('status')} {bridge.get('cwd', '')}" for bridge in await listBridges() if bridge.get("status") in ("active", "idle")]


def findMatchingRemoteSessions(sessions: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = query.lower()
    return [session for session in sessions if q in session.get("id", "").lower() or q in session.get("title", "").lower()]


def formatRemoteSessionCandidate(remote: dict[str, Any]) -> str:
    return f"{remote.get('id', '')[:8]}  {remote.get('title', ''):<24}  {remote.get('status', ''):<12}  {remote.get('updatedAt', '')[:19]}"


def _truncate_remote_summary(value: str, limit: int = 96) -> str:
    return value if len(value) <= limit else f"{value[:limit - 1]}..."


def _build_bridge_event_summary(event: str, data: dict[str, Any]) -> str:
    if isinstance(data.get("text"), str) and data["text"].strip():
        return f"{event}: {data['text']}"
    if isinstance(data.get("summary"), str) and data["summary"].strip():
        return f"{event}: {data['summary']}"
    return event
