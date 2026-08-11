from __future__ import annotations

import os
import uuid
from typing import Any

from .._utils import now_iso
from ..storage import load_remote_session, save_remote_session


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
    await save_remote_session(record)
    return record


async def updateRemoteSession(remoteSessionId: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    record = await load_remote_session(remoteSessionId)
    if not record:
        return None
    record.update(updates)
    record["updatedAt"] = updates.get("updatedAt") if updates.get("updatedAt") is not None else now_iso()
    await save_remote_session(record)
    return record


def findMatchingRemoteSessions(sessions: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return sessions
    scored = [(session, _score_remote_session(session, q)) for session in sessions]
    return [session for session, _score in sorted((entry for entry in scored if entry[1] > 0), key=lambda entry: (entry[1], entry[0].get("updatedAt", "")), reverse=True)]


def formatRemoteSessionCandidate(remote: dict[str, Any]) -> str:
    workspace = remote.get("workspaceName") if remote.get("workspaceName") is not None else os.path.basename(remote.get("cwd") or "")
    return "  ".join([remote.get("id", "")[:8], str(workspace).ljust(18), str(remote.get("gitBranch") or "").ljust(18), str(remote.get("status", "")).ljust(10), _truncate_remote_summary(str(remote.get("title") or "")).ljust(34), str(remote.get("updatedAt", ""))[:19].replace("T", " ")])


def _truncate_remote_summary(value: str, limit: int = 96) -> str:
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


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


__all__ = [
    "createRemoteSession",
    "findMatchingRemoteSessions",
    "formatRemoteSessionCandidate",
    "updateRemoteSession",
]
