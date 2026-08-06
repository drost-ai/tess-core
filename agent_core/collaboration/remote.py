from __future__ import annotations

import os
import json
import math
import sys
import threading
import uuid
from typing import Any

from .._utils import now_iso
from .bridge.admission import admitBridgeMessage as _admitBridgeMessage
from .bridge.runtime import createBridgeRuntimeController as _createBridgeRuntimeController
from ..runtime.paths import runWithRuntimePaths
from ..storage import (
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
    """Create the selected, durable bridge generation for a session."""

    return await _createBridgeRuntimeController({**(args or {}), **kwargs})


async def sendBridgeMessage(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Admit one generation-bound, idempotent bridge message."""

    return await _admitBridgeMessage({**(args or {}), **kwargs})


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
