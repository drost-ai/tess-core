from __future__ import annotations

import copy
import os
import re
from typing import Any

from ._utils import now_iso
from .storage import (
    appendSessionTranscriptLogEntries,
    createEmptySession,
    hasPersistableSessionMessages,
    listSessions,
    loadSession,
    saveSession,
)
from .workspace import captureWorkspaceSnapshot, hydrateSessionWorkspaceMetadata, parsePullRequestReference


async def resolveSession(options: dict[str, Any], config: dict[str, Any], launchContext: dict[str, Any] | None = None) -> dict[str, Any]:
    launch = launchContext or {"cwd": config["cwd"], "originalCwd": config["cwd"]}
    sessions = await listSessions()
    existing = await _resolve_existing_session(options, sessions, launch)
    if existing:
        selected = await loadSession(existing["id"])
        return _fork_or_reuse_session(selected or existing, options, config, launch)
    created = createEmptySession(
        cwd=launch["cwd"],
        provider=config["provider"],
        providerProfileName=config.get("providerProfileName"),
        model=config["model"],
        effort=config.get("effort"),
        systemPrompt=config["systemPrompt"],
        permissionMode=config["permissionMode"],
        name=config.get("name"),
        assistantName=config.get("assistantName"),
        assistantDescription=config.get("assistantDescription"),
        sessionId=options.get("sessionId"),
        originalCwd=launch["originalCwd"],
    )
    if launch.get("worktreePath"):
        created.setdefault("metadata", {})["worktreePath"] = launch["worktreePath"]
    await hydrateSessionWorkspaceMetadata(created)
    return created


async def persistSession(
    session: dict[str, Any],
    config: dict[str, Any],
    options: dict[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    if not config.get("sessionPersistence", True) or not hasPersistableSessionMessages(session):
        return
    snapshot = copy.deepcopy(session)
    repair = repairSessionTranscript(snapshot)
    await hydrateSessionWorkspaceMetadata(snapshot, {"includePullRequest": False})
    persist_options = {**(options or {}), **kwargs}
    if persist_options.get("touch", True):
        snapshot["updatedAt"] = now_iso()
    transcript_log, _appended = await _persist_session_transcript_log(snapshot, repair)
    snapshot.setdefault("metadata", {})["fullTranscriptLog"] = transcript_log
    await saveSession(snapshot)
    session.setdefault("metadata", {})["fullTranscriptLog"] = copy.deepcopy(transcript_log)
    session["updatedAt"] = snapshot["updatedAt"]


async def persistProviderFailure(session: dict[str, Any], config: dict[str, Any], failure: dict[str, Any]) -> None:
    if not config.get("sessionPersistence", True) or not hasPersistableSessionMessages(session):
        return
    metadata = session.setdefault("metadata", {})
    metadata["providerFailureCount"] = metadata.get("providerFailureCount", 0) + 1
    metadata["lastProviderFailure"] = copy.deepcopy(failure)
    await saveSession(copy.deepcopy(session))
    await appendSessionTranscriptLogEntries(session["id"], [{"type": "provider_failure", "session_id": session["id"], "persisted_at": now_iso(), "failure": failure}])


async def persistProviderRecovery(session: dict[str, Any], config: dict[str, Any], details: dict[str, Any]) -> None:
    if not config.get("sessionPersistence", True) or not hasPersistableSessionMessages(session):
        return
    session.setdefault("metadata", {})["lastProviderRecoveryAt"] = details["recoveredAt"]
    await saveSession(copy.deepcopy(session))
    await appendSessionTranscriptLogEntries(session["id"], [{"type": "provider_recovered", "session_id": session["id"], "persisted_at": details["recoveredAt"], **details}])


def repairSessionTranscript(session: dict[str, Any]) -> dict[str, Any]:
    invalid = _find_invalid_tool_use_boundary(session.get("messages", []))
    if not invalid:
        return {"repaired": False, "removedCount": 0, "missingToolUseIds": []}
    removed = len(session.get("messages", [])) - invalid["index"]
    session["messages"] = session.get("messages", [])[: invalid["index"]]
    return {"repaired": True, "truncatedFrom": invalid["index"], "removedCount": removed, "missingToolUseIds": invalid["missingToolUseIds"]}


def cloneSession(session: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    next_session = copy.deepcopy(session)
    if overrides:
        next_session.update(overrides)
    return next_session


def setSessionCwd(session: dict[str, Any], nextCwd: str) -> None:
    session["cwd"] = os.path.abspath(nextCwd)


def findMatchingSessions(sessions: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    normalized = query.strip().lower()
    if not normalized:
        return sessions
    pr_ref = parsePullRequestReference(query)
    scored = [(session, _score_session(session, normalized, pr_ref)) for session in sessions]
    return [item[0] for item in sorted([item for item in scored if item[1] > 0], key=lambda item: (item[1], item[0].get("updatedAt", "")), reverse=True)]


def formatSessionCandidate(session: dict[str, Any]) -> str:
    metadata = session.get("metadata", {})
    label = session.get("name") or metadata.get("workspaceName") or os.path.basename(session.get("cwd", ""))
    branch = metadata.get("gitBranch") or ""
    pr = f"PR #{metadata['prNumber']}" if metadata.get("prNumber") else ""
    return f"{session['id'][:8]}  {label:<18}  {branch:<18}  {pr:<10}  {session.get('updatedAt', '')[:19].replace('T', ' ')}".rstrip()


async def findSessionsForWorkspace(sessions: list[dict[str, Any]], cwd: str) -> list[dict[str, Any]]:
    snapshot = await captureWorkspaceSnapshot(cwd, {"includePullRequest": False})
    resolved_cwd = os.path.abspath(cwd)
    resolved_git = os.path.abspath(snapshot["gitRoot"]) if snapshot.get("gitRoot") else None
    return [session for session in sessions if _matches_workspace(session, resolved_cwd, resolved_git)]


async def _persist_session_transcript_log(
    session: dict[str, Any], repair: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    raw_previous_log = session.get("metadata", {}).get("fullTranscriptLog", {})
    previous_log = raw_previous_log if isinstance(raw_previous_log, dict) else {}
    previous_count = previous_log.get("archivedMessageCount", 0)
    previous = previous_count if isinstance(previous_count, int) and previous_count >= 0 else 0
    archived_at = now_iso()
    entries = []
    if repair.get("repaired") and previous > len(session.get("messages", [])):
        entries.append({"type": "repair", "session_id": session["id"], "persisted_at": archived_at, "truncated_to_message_index": len(session.get("messages", [])), "previous_archived_message_count": previous, "removed_count": repair["removedCount"], "missing_tool_use_ids": repair["missingToolUseIds"]})
    for index in range(min(previous, len(session.get("messages", []))), len(session.get("messages", []))):
        message = session["messages"][index]
        entries.append({"type": "message", "session_id": session["id"], "message_index": index, "persisted_at": archived_at, "role": message.get("role"), "content": message.get("content")})
    path = await appendSessionTranscriptLogEntries(session["id"], entries)
    appended = bool(entries)
    return (
        {
            "path": path,
            "archivedMessageCount": len(session.get("messages", [])),
            "lastArchivedAt": archived_at if appended else previous_log.get("lastArchivedAt"),
        },
        appended,
    )


def _find_invalid_tool_use_boundary(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        tool_ids = _collect_tool_use_ids(message)
        if not tool_ids:
            continue
        next_message = messages[index + 1] if index + 1 < len(messages) else None
        if not next_message or next_message.get("role") != "user" or not isinstance(next_message.get("content"), list):
            return {"index": index, "missingToolUseIds": tool_ids}
        result_ids = {block.get("tool_use_id") for block in next_message["content"] if block.get("type") == "tool_result"}
        missing = [tool_id for tool_id in tool_ids if tool_id not in result_ids]
        if missing:
            return {"index": index, "missingToolUseIds": missing}
    return None


def _collect_tool_use_ids(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block["id"] for block in content if block.get("type") == "tool_use" and block.get("id")]


async def _resolve_existing_session(options: dict[str, Any], sessions: list[dict[str, Any]], launch: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(options.get("resume"), str):
        matches = findMatchingSessions(sessions, options["resume"])
        if not matches:
            raise ValueError(f"No saved session matched: {options['resume']}")
        return matches[0]
    if options.get("continue"):
        matches = await findSessionsForWorkspace(sessions, launch["originalCwd"])
        return matches[0] if matches else None
    return None


def _fork_or_reuse_session(session: dict[str, Any], options: dict[str, Any], config: dict[str, Any], launch: dict[str, Any]) -> dict[str, Any]:
    if not options.get("forkSession"):
        if options.get("name"):
            session["name"] = options["name"]
        if launch.get("worktreePath"):
            session["cwd"] = launch["cwd"]
            session.setdefault("metadata", {})["originalCwd"] = launch["originalCwd"]
            session["metadata"]["worktreePath"] = launch["worktreePath"]
        return session
    forked = cloneSession(session, {"id": options.get("sessionId") or __import__("uuid").uuid4().hex, "createdAt": now_iso(), "updatedAt": now_iso(), "cwd": launch["cwd"], "name": options.get("name") or session.get("name"), "model": config["model"], "effort": config.get("effort"), "systemPrompt": config["systemPrompt"], "permissionMode": config["permissionMode"]})
    metadata = forked.setdefault("metadata", {})
    metadata["originalCwd"] = launch["originalCwd"]
    metadata["forkedFromSessionId"] = session["id"]
    metadata["childSessionIds"] = []
    metadata["childAgentIds"] = []
    metadata["planMode"] = {"state": "inactive"}
    metadata["depth"] = 0
    metadata["usage"] = {"turns": 0, "toolCalls": 0, "inputTokens": 0, "outputTokens": 0, "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0}
    return forked


def _matches_workspace(session: dict[str, Any], resolved_cwd: str, resolved_git: str | None = None) -> bool:
    metadata = session.get("metadata", {})
    paths = [session.get("cwd"), metadata.get("originalCwd"), metadata.get("gitRoot")]
    normalized = [os.path.abspath(path) for path in paths if path]
    return resolved_cwd in normalized or bool(resolved_git and resolved_git in normalized)


def _score_session(session: dict[str, Any], query: str, pr_ref: dict[str, Any]) -> int:
    metadata = session.get("metadata", {})
    candidates = {
        "id": session.get("id", "").lower(),
        "name": (session.get("name") or "").lower(),
        "cwd": session.get("cwd", "").lower(),
        "original": (metadata.get("originalCwd") or "").lower(),
        "workspace": (metadata.get("workspaceName") or "").lower(),
        "branch": (metadata.get("gitBranch") or "").lower(),
        "prUrl": (metadata.get("prUrl") or "").lower(),
    }
    score = 0
    if candidates["id"] == query:
        score = max(score, 100)
    elif candidates["id"].startswith(query):
        score = max(score, 95)
    for key, exact, partial in (("name", 92, 80), ("workspace", 84, 72), ("branch", 78, 68)):
        if candidates[key] == query:
            score = max(score, exact)
        elif query in candidates[key]:
            score = max(score, partial)
    if query in candidates["cwd"] or query in candidates["original"]:
        score = max(score, 70)
    if pr_ref.get("prNumber") and metadata.get("prNumber") == pr_ref["prNumber"]:
        score = max(score, 96)
    if pr_ref.get("prUrl") and pr_ref["prUrl"].lower() in candidates["prUrl"]:
        score = max(score, 94)
    return score
