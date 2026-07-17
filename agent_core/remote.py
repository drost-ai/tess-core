from __future__ import annotations

import os
import json
import math
import sys
import threading
import uuid
from typing import Any

from ._utils import now_iso
from .runtime_paths import runWithRuntimePaths
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
    cwd = args.get("cwd") or session.get("cwd")
    record = {
        "id": args.get("id") or str(uuid.uuid4()),
        "sessionId": args.get("sessionId") or session.get("id"),
        "title": args.get("title") or _derive_remote_title(args.get("description"), session, cwd),
        "description": args.get("description"),
        "createdAt": now,
        "updatedAt": now,
        "status": "created",
        "cwd": cwd,
        "originalCwd": args.get("originalCwd") if args.get("originalCwd") is not None else metadata.get("originalCwd"),
        "workspaceName": metadata.get("workspaceName"),
        "gitRoot": metadata.get("gitRoot"),
        "gitBranch": metadata.get("gitBranch"),
        "prNumber": metadata.get("prNumber"),
        "prUrl": metadata.get("prUrl"),
        "channelServers": list(args.get("channelServers", [])),
        "unreadBridgeMessages": 0,
        "foregroundSessionId": args.get("sessionId") or session.get("id"),
    }
    await saveRemoteSession(record)
    return record


async def updateRemoteSession(remoteSessionId: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    record = await loadRemoteSession(remoteSessionId)
    if not record:
        return None
    record.update(updates)
    record["updatedAt"] = updates.get("updatedAt") if updates.get("updatedAt") is not None else now_iso()
    await saveRemoteSession(record)
    return record


async def resolveTeleportSession(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
    args = {**(args or {}), **kwargs}
    teleport = args.get("teleport")
    options = args.get("options") or {}
    if not teleport:
        return None
    sessions = await listRemoteSessions()
    if not sessions:
        raise RuntimeError("No remote sessions are available for teleport.")
    if isinstance(teleport, bool):
        if not canPromptForSelection(options):
            raise RuntimeError(
                "`--teleport` without a value requires an interactive terminal or an explicit session ID."
            )
        selected = await promptToPickRemoteSession(sessions, "Select a remote session to resume:")
        if not selected:
            raise RuntimeError("No remote session was selected.")
        return {"remote": selected, "sessionId": selected.get("sessionId")}
    matches = findMatchingRemoteSessions(sessions, str(teleport))
    if not matches:
        raise RuntimeError(f"No remote session matched: {teleport}")
    selected = await resolveRemoteSelection(matches, str(teleport), options)
    if not selected:
        raise RuntimeError("No remote session was selected.")
    return {"remote": selected, "sessionId": selected.get("sessionId")}


async def createBridgeRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args.get("session") or {}
    emit = args.get("emit") or (lambda *_: None)
    runtime_paths = args.get("runtimePaths")
    now = now_iso()
    requested_bridge_id = args.get("bridgeId") or args.get("bridge_id")
    existing_bridge = await loadBridge(requested_bridge_id) if requested_bridge_id else None
    if requested_bridge_id and not existing_bridge:
        raise RuntimeError(f"Bridge not found for reconnect: {requested_bridge_id}")

    if existing_bridge:
        bridge = {
            **existing_bridge,
            "sessionId": args.get("sessionId") or session.get("id") or existing_bridge.get("sessionId"),
            "remoteSessionId": args.get("remoteSessionId") if args.get("remoteSessionId") is not None else existing_bridge.get("remoteSessionId"),
            "name": args.get("name") if args.get("name") is not None else existing_bridge.get("name"),
            "cwd": args.get("cwd") or session.get("cwd") or existing_bridge.get("cwd"),
            "updatedAt": now,
            "status": "active",
            "lastEventName": "bridge.reconnected",
        }
    else:
        bridge = {"id": args.get("id") or str(uuid.uuid4()), "sessionId": args.get("sessionId") or session.get("id"), "remoteSessionId": args.get("remoteSessionId"), "name": args.get("name"), "createdAt": now, "updatedAt": now, "status": "active", "cwd": args.get("cwd") or session.get("cwd"), "lastEventName": "bridge.started", "pendingMessages": 0, "messageCount": 0}
    await saveBridge(bridge)
    if bridge.get("remoteSessionId"):
        await updateRemoteSession(bridge["remoteSessionId"], {"bridgeId": bridge["id"], "status": "active", "bridgeStatus": "active", "lastEventAt": now, "lastEventSummary": "Bridge reconnected" if existing_bridge else "Bridge attached"})

    async def record_event(event: str, data: dict[str, Any] | None = None) -> None:
        timestamp = now_iso()
        await appendBridgeEvent({"id": str(uuid.uuid4()), "bridgeId": bridge["id"], "createdAt": timestamp, "event": event, "data": data or {}})
        current = await loadBridge(bridge["id"])
        if current:
            current.update({"updatedAt": timestamp, "lastEventName": event})
            await saveBridge(current)
        if bridge.get("remoteSessionId"):
            await updateRemoteSession(bridge["remoteSessionId"], {"status": "idle" if event == "bridge.closed" else "completed" if event == "session.completed" else "active", "bridgeStatus": "closed" if event == "bridge.closed" else "active", "lastEventName": event, "lastEventAt": timestamp, "lastEventSummary": _truncate_remote_summary(_build_bridge_event_summary(event, data or {}))})

    async def record_failure(error: Exception, phase: str) -> None:
        timestamp = now_iso()
        message = str(error) or error.__class__.__name__
        payload = {"bridge_id": bridge["id"], "session_id": bridge.get("sessionId"), "remote_session_id": bridge.get("remoteSessionId"), "phase": phase, "error": message}
        current = await loadBridge(bridge["id"])
        if current:
            current.update({"status": "failed", "updatedAt": timestamp, "lastEventName": "bridge.failed", "lastFailurePhase": phase, "lastFailureMessage": message})
            await saveBridge(current)
        await appendBridgeEvent({"id": str(uuid.uuid4()), "bridgeId": bridge["id"], "createdAt": timestamp, "event": "bridge.failed", "data": payload})
        if bridge.get("remoteSessionId"):
            await updateRemoteSession(bridge["remoteSessionId"], {"status": "failed", "bridgeStatus": "failed", "lastEventName": "bridge.failed", "lastEventAt": timestamp, "lastEventSummary": _truncate_remote_summary(f"Bridge failed during {phase}: {message}")})

    closed = False
    existing_inbox = await loadBridgeInbox(bridge["id"]) if existing_bridge else []
    inbox_cursor = min(int(bridge.get("messageCount") or 0), len(existing_inbox)) if existing_bridge else 0
    pending_replay_count = max(0, len(existing_inbox) - inbox_cursor)
    poll_lock = threading.Lock()
    stop_event = threading.Event()
    poll_interval = float(args.get("pollIntervalSeconds", 0.75))
    poll_thread: threading.Thread | None = None

    async def poll_bridge_inbox() -> list[dict[str, Any]]:
        nonlocal inbox_cursor
        if closed:
            return []
        if not poll_lock.acquire(blocking=False):
            return []
        try:
            try:
                start_cursor = inbox_cursor
                inbox = await loadBridgeInbox(bridge["id"])
                pending = inbox[start_cursor:]
                if not pending:
                    return []
                inbox_cursor = len(inbox)
                delivered = []
                for message in pending:
                    payload = {"bridge_id": bridge["id"], "session_id": bridge.get("sessionId"), "remote_session_id": bridge.get("remoteSessionId"), "message_id": message.get("id"), "source": message.get("source"), "text": message.get("text")}
                    emit("bridge.message", payload)
                    current_bridge = await loadBridge(bridge["id"])
                    timestamp = now_iso()
                    if current_bridge:
                        current_bridge.update(
                            {
                                "status": "active",
                                "updatedAt": timestamp,
                                "lastEventName": "bridge.message.received",
                                "lastMessageAt": message.get("createdAt"),
                                "lastMessageSource": message.get("source"),
                                "pendingMessages": max(0, int(current_bridge.get("pendingMessages") or 1) - 1),
                                "messageCount": int(current_bridge.get("messageCount") or 0) + 1,
                            }
                        )
                        await saveBridge(current_bridge)
                    await appendBridgeEvent({"id": str(uuid.uuid4()), "bridgeId": bridge["id"], "createdAt": timestamp, "event": "bridge.message.received", "data": {"message_id": message.get("id"), "source": message.get("source"), "text": message.get("text")}})
                    if bridge.get("remoteSessionId"):
                        await updateRemoteSession(bridge["remoteSessionId"], {"status": "awaiting_input", "bridgeStatus": "active", "lastEventName": "bridge.message.received", "lastEventAt": now_iso(), "lastEventSummary": _truncate_remote_summary(f"Bridge message: {message.get('text')}")})
                    delivered.append(message)
                return delivered
            except Exception as exc:
                inbox_cursor = start_cursor
                await record_failure(exc, "poll")
                raise
        finally:
            poll_lock.release()

    def poll_loop() -> None:
        while not stop_event.wait(poll_interval):
            try:
                import asyncio

                work = lambda: poll_bridge_inbox()
                asyncio.run(runWithRuntimePaths(runtime_paths, work) if runtime_paths else work())
            except Exception:
                stop_event.wait(poll_interval)

    async def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        stop_event.set()
        if poll_thread and poll_thread.is_alive() and threading.current_thread() is not poll_thread:
            poll_thread.join(timeout=2)
        timestamp = now_iso()
        current = await loadBridge(bridge["id"])
        if current:
            current.update({"status": "closed", "updatedAt": timestamp, "lastEventName": "bridge.closed"})
            await saveBridge(current)
        if bridge.get("remoteSessionId"):
            await updateRemoteSession(bridge["remoteSessionId"], {"status": "idle", "bridgeStatus": "closed", "lastEventName": "bridge.closed", "lastEventAt": timestamp, "lastEventSummary": "Bridge closed"})

    start_event = "bridge.reconnected" if existing_bridge else "bridge.started"
    start_payload = {"bridge_id": bridge["id"], "session_id": bridge.get("sessionId"), "remote_session_id": bridge.get("remoteSessionId"), "name": bridge.get("name")}
    if existing_bridge:
        start_payload["replayed_messages"] = pending_replay_count
    await record_event(start_event, start_payload)
    emit(start_event, start_payload)
    if args.get("startPolling", True):
        poll_thread = threading.Thread(target=poll_loop, name=f"agent-core-bridge-{bridge['id'][:8]}", daemon=True)
        poll_thread.start()
    return {"bridge": bridge, "send": lambda text: sendBridgeMessage({"bridgeId": bridge["id"], "text": text}), "recordEvent": record_event, "pollInbox": poll_bridge_inbox, "pollBridgeInbox": poll_bridge_inbox, "diagnostics": lambda: getBridgeDiagnostics(bridge["id"]), "getDiagnostics": lambda: getBridgeDiagnostics(bridge["id"]), "close": close}


async def sendBridgeMessage(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    bridge = await loadBridge(args["bridgeId"])
    if not bridge:
        raise RuntimeError(f"Bridge not found: {args['bridgeId']}")
    if bridge.get("status") != "active":
        raise RuntimeError(f"Bridge is not active: {args['bridgeId']}")
    source = args.get("source") if args.get("source") is not None else "bridge"
    message = {"id": str(uuid.uuid4()), "bridgeId": args["bridgeId"], "createdAt": now_iso(), "source": source, "text": args["text"]}
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
    lines = []
    for item in events[-limit:]:
        data = item.get("data") or {}
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            summary = _truncate_remote_summary(data["text"])
        else:
            summary = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        lines.append(f"{item.get('createdAt')}  {item.get('event')}  {summary}")
    return "\n".join(lines)


async def getBridgeDiagnostics(bridgeId: str, limit: int = 20) -> dict[str, Any]:
    bridge = await loadBridge(bridgeId)
    events = await loadBridgeEvents(bridgeId)
    inbox = await loadBridgeInbox(bridgeId)
    remote = await loadRemoteSession(bridge.get("remoteSessionId")) if bridge and bridge.get("remoteSessionId") else None
    return {
        "bridge": bridge,
        "remote": remote,
        "status": (bridge or {}).get("status"),
        "remoteStatus": (remote or {}).get("status"),
        "bridgeStatus": (remote or {}).get("bridgeStatus"),
        "pendingMessages": (bridge or {}).get("pendingMessages") if bridge else 0,
        "messageCount": (bridge or {}).get("messageCount") if bridge else 0,
        "inboxCount": len(inbox),
        "events": events[-limit:],
    }


async def listActiveBridgeRows() -> list[str]:
    rows = []
    for bridge in await listBridges():
        if bridge.get("status") != "active":
            continue
        name = bridge.get("name") if bridge.get("name") is not None else os.path.basename(bridge.get("cwd") or "")
        remote = bridge.get("remoteSessionId")[:8] if bridge.get("remoteSessionId") else "-"
        rows.append("  ".join([bridge.get("id", "")[:8], str(name).ljust(18), str(bridge.get("status", "")).ljust(8), remote, str(bridge.get("updatedAt", ""))[:19].replace("T", " ")]))
    return rows


def findMatchingRemoteSessions(sessions: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return sessions
    scored = [(session, _score_remote_session(session, q)) for session in sessions]
    return [session for session, _score in sorted((entry for entry in scored if entry[1] > 0), key=lambda entry: (entry[1], entry[0].get("updatedAt", "")), reverse=True)]


def formatRemoteSessionCandidate(remote: dict[str, Any]) -> str:
    workspace = remote.get("workspaceName") if remote.get("workspaceName") is not None else os.path.basename(remote.get("cwd") or "")
    return "  ".join([remote.get("id", "")[:8], str(workspace).ljust(18), str(remote.get("gitBranch") or "").ljust(18), str(remote.get("status", "")).ljust(10), _truncate_remote_summary(str(remote.get("title") or "")).ljust(34), str(remote.get("updatedAt", ""))[:19].replace("T", " ")])


def canPromptForSelection(options: dict[str, Any] | None = None) -> bool:
    options = options or {}
    return not options.get("print") and bool(sys.stdin.isatty() and sys.stdout.isatty())


async def resolveRemoteSelection(
    matches: list[dict[str, Any]],
    query: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if len(matches) == 1:
        return matches[0]
    if not canPromptForSelection(options):
        raise RuntimeError(
            "\n".join(
                [
                    f'Multiple remote sessions matched "{query}".',
                    *[f"  {formatRemoteSessionCandidate(remote)}" for remote in matches[:10]],
                ]
            )
        )
    return await promptToPickRemoteSession(matches, f'Select a remote session matching "{query}":')


async def promptToPickRemoteSession(
    remoteSessions: list[dict[str, Any]],
    prompt: str,
) -> dict[str, Any] | None:
    sys.stdout.write(f"\n{prompt}\n")
    for index, remote in enumerate(remoteSessions[:12]):
        sys.stdout.write(f"  {index + 1}. {formatRemoteSessionCandidate(remote)}\n")
    answer = input("choice> ").strip()
    if not answer:
        return None
    numeric = _parse_integer_selection(answer)
    if numeric is not None and 1 <= numeric <= len(remoteSessions):
        return remoteSessions[numeric - 1]
    matches = findMatchingRemoteSessions(remoteSessions, answer)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError(
            "\n".join(
                [
                    f'Multiple remote sessions still matched "{answer}".',
                    *[f"  {formatRemoteSessionCandidate(remote)}" for remote in matches[:10]],
                ]
            )
        )
    raise RuntimeError(f'No remote session matched "{answer}".')


def _truncate_remote_summary(value: str, limit: int = 96) -> str:
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


def _parse_integer_selection(value: str) -> int | None:
    try:
        numeric = float(value)
    except ValueError:
        return None
    if not math.isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)


def _build_bridge_event_summary(event: str, data: dict[str, Any]) -> str:
    if isinstance(data.get("text"), str) and data["text"].strip():
        return f"{event}: {data['text']}"
    if isinstance(data.get("summary"), str) and data["summary"].strip():
        return f"{event}: {data['summary']}"
    return event


def _derive_remote_title(description: str | None, session: dict[str, Any], cwd: str | None) -> str:
    trimmed = description.strip() if isinstance(description, str) else ""
    if trimmed:
        return _truncate_remote_summary(trimmed, 72)
    metadata = session.get("metadata", {}) if isinstance(session, dict) else {}
    if session.get("name") is not None:
        return session.get("name")
    if metadata.get("workspaceName") is not None:
        return metadata.get("workspaceName")
    return os.path.basename(cwd or "")


def _score_remote_session(remote: dict[str, Any], query: str) -> int:
    score = 0
    remote_id = str(remote.get("id") or "").lower()
    session_id = str(remote.get("sessionId") or "").lower()
    title = str(remote.get("title") or "").lower()
    description = str(remote.get("description") or "").lower()
    workspace = str(remote.get("workspaceName") or "").lower()
    branch = str(remote.get("gitBranch") or "").lower()
    if remote_id == query:
        score = max(score, 100)
    elif remote_id.startswith(query):
        score = max(score, 94)
    if session_id.startswith(query):
        score = max(score, 86)
    if title == query:
        score = max(score, 92)
    elif query in title:
        score = max(score, 80)
    if query in description:
        score = max(score, 76)
    if workspace == query:
        score = max(score, 84)
    elif query in workspace:
        score = max(score, 70)
    if branch == query:
        score = max(score, 74)
    elif query in branch:
        score = max(score, 66)
    return score
