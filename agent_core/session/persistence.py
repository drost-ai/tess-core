from __future__ import annotations

import copy
import os
import re
from typing import Any

from .._utils import now_iso
from ..config.prompts import buildEffectiveSystemPrompt
from ..storage.persistence_metrics import (
    beginPersistenceCall as _begin_persistence_call,
    finishPersistenceCall as _finish_persistence_call,
)
from .fork import createForkArchiveHeader, createForkedSession
from .message_history import (
    SessionMessageList,
    createSessionMessageList as _create_session_message_list,
    sessionPersistenceCheckpoint as _session_persistence_checkpoint,
    setSessionPersistenceCheckpoint as _set_session_persistence_checkpoint,
)
from .tool_boundaries import findIncompleteToolExchange as _find_incomplete_tool_exchange
from ..storage import (
    appendSessionTranscriptLogEntries,
    createEmptySession,
    hasPersistableSessionMessages,
    listSessions,
    loadSession,
    mutateSessionHeadDeltaTransaction,
    mutateSessionRecordTransaction,
)
from ..runtime.workspace import captureWorkspaceSnapshot, hydrateSessionWorkspaceMetadata, parsePullRequestReference


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
        systemPrompt=buildEffectiveSystemPrompt(config),
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
    measurement = _begin_persistence_call(str(session.get("id") or ""), len(session.get("messages") or []))
    try:
        persist_options = {**(options or {}), **kwargs}
        if not config.get("sessionPersistence", True) or (
            not hasPersistableSessionMessages(session)
            and not persist_options.get("force")
        ):
            _finish_persistence_call(measurement, "skipped")
            return None
        checkpoint_before = _session_persistence_checkpoint(session)
        snapshot = _copy_session_for_persist(session)
        repair = repairSessionTranscript(snapshot)
        await hydrateSessionWorkspaceMetadata(snapshot, {"includePullRequest": False})
        if persist_options.get("touch", True):
            snapshot["updatedAt"] = now_iso()
        transcript_log, _appended = await _persist_session_transcript_log(
            snapshot,
            repair,
            truncate=bool(persist_options.get("truncateTranscript")),
        )
        snapshot.setdefault("metadata", {})["fullTranscriptLog"] = transcript_log
        committed = await _commit_prepared_session_snapshot(
            snapshot,
            fault_injector=persist_options.get("faultInjector"),
        )
        _sync_committed_session_state(
            session,
            committed,
            message_source=snapshot.get("messages"),
        )
        if repair.get("repaired"):
            _adopt_repaired_checkpoint(session, snapshot, committed, checkpoint_before)
        # Returns nothing, like the TS peer: building a sanitized copy of the
        # committed record cost an O(history) walk per persist that no
        # production caller ever read.
        _finish_persistence_call(measurement, "persisted", revision=committed["revision"])
    except BaseException:
        _finish_persistence_call(measurement, "error")
        raise


async def persistProviderFailure(session: dict[str, Any], config: dict[str, Any], failure: dict[str, Any]) -> None:
    if not config.get("sessionPersistence", True) or not hasPersistableSessionMessages(session):
        return
    metadata = session.setdefault("metadata", {})
    metadata["providerFailureCount"] = metadata.get("providerFailureCount", 0) + 1
    metadata["lastProviderFailure"] = copy.deepcopy(failure)
    interrupted = failure.get("interruptedPreview")
    if isinstance(interrupted, dict):
        metadata["lastAssistantInterruptedPreview"] = copy.deepcopy(interrupted)
    snapshot = _copy_session_for_persist(session)
    committed = await _commit_prepared_session_snapshot(snapshot)
    _sync_committed_session_state(session, committed, message_source=snapshot.get("messages"))
    persisted_at = now_iso()
    entries = [{"type": "provider_failure", "session_id": session["id"], "persisted_at": persisted_at, "failure": failure}]
    if isinstance(interrupted, dict):
        entries.append(
            {
                "type": "assistant_interrupted_preview",
                "session_id": session["id"],
                "persisted_at": persisted_at,
                "receipt": copy.deepcopy(interrupted),
            }
        )
    await appendSessionTranscriptLogEntries(session["id"], entries)


async def persistProviderRecovery(session: dict[str, Any], config: dict[str, Any], details: dict[str, Any]) -> None:
    if not config.get("sessionPersistence", True) or not hasPersistableSessionMessages(session):
        return
    session.setdefault("metadata", {})["lastProviderRecoveryAt"] = details["recoveredAt"]
    snapshot = _copy_session_for_persist(session)
    committed = await _commit_prepared_session_snapshot(snapshot)
    _sync_committed_session_state(session, committed, message_source=snapshot.get("messages"))
    await appendSessionTranscriptLogEntries(session["id"], [{"type": "provider_recovered", "session_id": session["id"], "persisted_at": details["recoveredAt"], **details}])


_CONCURRENT_SESSION_ID_FIELDS = (
    "taskIds",
    "monitorIds",
    "backgroundSessionIds",
    "childSessionIds",
    "childAgentIds",
)
_CONCURRENT_SESSION_SCALAR_FIELDS = (
    "subagentTaskId",
    "subagentLaunchGroupId",
    "subagentRuntimeContract",
    "daemonUnavailable",
)


def _valid_revision(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _merge_concurrent_operational_state(
    desired: dict[str, Any],
    latest: dict[str, Any] | None,
) -> dict[str, Any]:
    if latest is None:
        return copy.deepcopy(desired)
    desired_revision = _valid_revision(desired.get("revision"))
    latest_revision = _valid_revision(latest.get("revision"))
    merged = copy.deepcopy(desired)
    if "revision" in latest:
        merged["revision"] = latest["revision"]
    else:
        merged.pop("revision", None)
    if desired_revision == latest_revision:
        return merged
    desired_metadata = merged.setdefault("metadata", {})
    latest_metadata = latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
    for field in _CONCURRENT_SESSION_ID_FIELDS:
        desired_metadata[field] = list(dict.fromkeys([
            *(latest_metadata.get(field) or []),
            *(desired_metadata.get(field) or []),
        ]))
    for field in _CONCURRENT_SESSION_SCALAR_FIELDS:
        if field in latest_metadata:
            desired_metadata[field] = copy.deepcopy(latest_metadata[field])
    return merged


async def _commit_prepared_session_snapshot(
    snapshot: dict[str, Any], *, fault_injector: Any = None
) -> dict[str, Any]:
    messages = snapshot.get("messages")
    desired_head = {
        key: value for key, value in snapshot.items() if key != "messages"
    }

    async def commit_head(latest: dict[str, Any]) -> dict[str, Any]:
        merged = _merge_concurrent_operational_state(desired_head, latest)
        return {"changed": merged != latest, "session": merged}

    if isinstance(messages, list):
        fast = await mutateSessionHeadDeltaTransaction(
            snapshot["id"],
            messages,
            commit_head,
            faultInjector=fault_injector,
        )
        if fast is not None:
            return fast

    async def commit(latest: dict[str, Any] | None) -> dict[str, Any]:
        merged = _merge_concurrent_operational_state(snapshot, latest)
        return {"changed": latest is None or merged != latest, "session": merged}

    return await mutateSessionRecordTransaction(
        snapshot["id"], commit, faultInjector=fault_injector
    )


def _copy_session_for_persist(session: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value if key == "messages" else copy.deepcopy(value)
        for key, value in session.items()
    }


def _sync_committed_session_state(
    target: dict[str, Any],
    committed: dict[str, Any],
    *,
    message_source: Any = None,
) -> None:
    persisted = committed["session"]
    target["revision"] = committed["revision"]
    target_metadata = target.setdefault("metadata", {})
    persisted_metadata = persisted.get("metadata") if isinstance(persisted.get("metadata"), dict) else {}
    if "fullTranscriptLog" in persisted_metadata:
        target_metadata["fullTranscriptLog"] = copy.deepcopy(persisted_metadata["fullTranscriptLog"])
    for field in _CONCURRENT_SESSION_ID_FIELDS:
        target_metadata[field] = list(dict.fromkeys([
            *(target_metadata.get(field) or []),
            *(persisted_metadata.get(field) or []),
        ]))
    for field in _CONCURRENT_SESSION_SCALAR_FIELDS:
        if field not in target_metadata and field in persisted_metadata:
            target_metadata[field] = copy.deepcopy(persisted_metadata[field])
    target["updatedAt"] = persisted["updatedAt"]
    if target.get("messages") is message_source:
        _set_session_persistence_checkpoint(
            target,
            committed["revision"],
            len(target.get("messages") or []),
            committed.get("bodyCommitId"),
        )


def _adopt_repaired_checkpoint(
    session: dict[str, Any],
    snapshot: dict[str, Any],
    committed: dict[str, Any],
    checkpoint_before: Any,
) -> None:
    """After a repair-persist, stamp the commit's watermark on the live list.

    The repaired snapshot holds a different list object, so the identity check
    in _sync_committed_session_state cannot refresh the live checkpoint — and
    a stale one forces the next persist into the full fallback. The commit
    covered exactly the live prefix the truncated copy was taken from, so the
    watermark transfers whenever the live lineage is unchanged since capture.
    """
    live = session.get("messages")
    covered = len(snapshot.get("messages") or [])
    if (
        not isinstance(live, SessionMessageList)
        or checkpoint_before is None
        or live.checkpoint() != checkpoint_before
        or covered < checkpoint_before.messageCount
        or covered > len(live)
        or committed.get("bodyCommitId") is None
    ):
        return
    live.set_checkpoint(
        _valid_revision(committed.get("revision")), covered, committed["bodyCommitId"]
    )


def repairSessionTranscript(session: dict[str, Any]) -> dict[str, Any]:
    invalid = _find_invalid_tool_use_boundary(session.get("messages", []))
    if not invalid:
        return {"repaired": False, "removedCount": 0, "missingToolUseIds": []}
    messages = session.get("messages", [])
    removed = len(messages) - invalid["index"]
    # A dangling in-flight tail is the routine mid-batch case: dropping only
    # unpersisted messages keeps the checkpoint, so the commit stays on the
    # head/delta fast path instead of a full-body fallback.
    session["messages"] = (
        messages.truncated_copy(invalid["index"])
        if isinstance(messages, SessionMessageList)
        else _create_session_message_list(messages[: invalid["index"]])
    )
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
    session: dict[str, Any], repair: dict[str, Any], *, truncate: bool = False
) -> tuple[dict[str, Any], bool]:
    raw_previous_log = session.get("metadata", {}).get("fullTranscriptLog", {})
    previous_log = raw_previous_log if isinstance(raw_previous_log, dict) else {}
    previous_count = previous_log.get("archivedMessageCount", 0)
    previous = previous_count if isinstance(previous_count, int) and previous_count >= 0 else 0
    archived_at = now_iso()
    entries = []
    fork_header = createForkArchiveHeader(session, archived_at)
    if fork_header:
        entries.append(fork_header)
    if (repair.get("repaired") or truncate) and previous > len(session.get("messages", [])):
        entries.append({"type": "repair", "session_id": session["id"], "persisted_at": archived_at, "truncated_to_message_index": len(session.get("messages", [])), "previous_archived_message_count": previous, "removed_count": repair.get("removedCount") or previous - len(session.get("messages", [])), "missing_tool_use_ids": repair.get("missingToolUseIds") or []})
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
    return _find_incomplete_tool_exchange(messages)


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
    timestamp = now_iso()
    return createForkedSession(
        parent=session,
        id=options.get("sessionId") or __import__("uuid").uuid4().hex,
        now=timestamp,
        cwd=launch["cwd"],
        originalCwd=launch["originalCwd"],
        worktreePath=launch.get("worktreePath"),
        tmuxSessionName=launch.get("tmuxSessionName"),
        tmuxWindowName=launch.get("tmuxWindowName"),
        tmuxMode=launch.get("tmuxMode"),
        name=options.get("name") or session.get("name"),
        model=config["model"],
        effort=config.get("effort"),
        systemPrompt=buildEffectiveSystemPrompt(config),
        permissionMode=config["permissionMode"],
        assistantName=config.get("assistantName"),
        assistantDescription=config.get("assistantDescription"),
    )


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
