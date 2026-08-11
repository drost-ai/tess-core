from __future__ import annotations

import copy
from typing import Any, Callable

from ..payloads.media_persistence import sanitizeMediaForPersistence as _sanitize_media_for_persistence
from ..runtime.identifiers import parseSessionId
from ..session.message_history import (
    SessionMessageList,
    setSessionPersistenceCheckpoint as _set_session_persistence_checkpoint,
)
from .json_io import _storage_lock_timeout_ms, readJsonFileTolerant, withRecordTransaction
from .paths import sessionPath
from .session_schema_v2 import (
    commitSessionV2,
    commitSessionV2Delta,
    hydratePersistedSession,
    isSessionHeadV2,
    sessionLiveHeadFromV2,
)


class SessionRecordNotFoundError(RuntimeError):
    pass


def normalizeSessionRevision(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


async def updateSessionRecord(
    sessionId: str,
    mutation: Callable[[dict[str, Any]], Any],
    options: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    async def require_latest(latest: dict[str, Any] | None) -> dict[str, Any]:
        if latest is None:
            raise SessionRecordNotFoundError(f"Session not found: {parseSessionId(sessionId)}")
        # The engine already hands the mutation a private deepcopy of the
        # persisted record; copying it again here was pure waste.
        return await _await_mutation(mutation(latest))

    return await mutateSessionRecordTransaction(sessionId, require_latest, options, **kwargs)


async def mutateSessionRecordTransaction(
    sessionId: str,
    mutation: Callable[[dict[str, Any] | None], Any],
    options: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    settings = {**(options or {}), **kwargs}
    timeout_ms = int(settings.get("timeoutMs") or _storage_lock_timeout_ms())

    async with withRecordTransaction("session", session_id, timeoutMs=timeout_ms):
        raw = await readJsonFileTolerant(sessionPath(session_id), None)
        hydrated = await hydratePersistedSession(session_id, raw)
        latest = hydrated["record"]
        if latest is not None and latest.get("id") != session_id:
            raise ValueError(
                f"Session transaction record identity mismatch ({session_id} != {latest.get('id')})."
            )
        current_revision = normalizeSessionRevision((latest or {}).get("revision"))
        result = await _await_mutation(mutation(copy.deepcopy(latest)))
        if not isinstance(result, dict) or "session" not in result or "changed" not in result:
            raise TypeError("Session mutation must return {'session': ..., 'changed': bool}.")
        next_session = result["session"]
        if not isinstance(next_session, dict):
            raise TypeError("Session mutation result must contain a session record.")
        if next_session.get("id") != session_id:
            raise ValueError(
                f"Session transaction cannot change identity ({session_id} -> {next_session.get('id')})."
            )
        changed = bool(result["changed"])
        if not changed:
            if latest is None:
                raise ValueError(f"Session transaction cannot no-op a missing record: {session_id}.")
            if not isSessionHeadV2(raw):
                commit = await commitSessionV2(
                    session_id,
                    raw,
                    latest,
                    _sanitize_media_for_persistence(latest),
                    faultInjector=settings.get("faultInjector"),
                    presanitized=True,
                )
                session = commit["session"]
                return {
                    "changed": True,
                    "revision": normalizeSessionRevision(session.get("revision")),
                    "session": session,
                    "bodyCommitId": commit["bodyCommitId"],
                }
            return {
                "changed": hydrated["changed"],
                "revision": normalizeSessionRevision(latest.get("revision")),
                "session": latest,
                "bodyCommitId": raw.get("bodyRef", {}).get("commitId"),
            }
        # This copy is load-bearing: persistence's save path deliberately
        # shares the live session's message list by reference, so the commit
        # must decouple before stamping the revision and sanitizing.
        committed = copy.deepcopy(next_session)
        committed["revision"] = current_revision + 1
        safe = _sanitize_media_for_persistence(committed)
        commit = await commitSessionV2(
            session_id,
            raw,
            latest,
            safe,
            faultInjector=settings.get("faultInjector"),
            presanitized=True,
        )
        session = commit["session"]
        return {
            "changed": True,
            "revision": normalizeSessionRevision(session.get("revision")),
            "session": session,
            "bodyCommitId": commit["bodyCommitId"],
        }


async def mutateSessionHeadDeltaTransaction(
    sessionId: str,
    messages: list[Any],
    mutation: Callable[[dict[str, Any]], Any],
    options: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Use a tracked append watermark to avoid loading the persisted body.

    ``None`` means the caller must use the full transaction. This is a normal,
    fail-safe result for v1 records, stale revisions, replaced lists, prefix
    edits, and other histories without owned append authority.
    """

    if not isinstance(messages, SessionMessageList):
        return None
    checkpoint = messages.checkpoint()
    if checkpoint is None:
        return None
    session_id = parseSessionId(sessionId)
    settings = {**(options or {}), **kwargs}
    timeout_ms = int(settings.get("timeoutMs") or _storage_lock_timeout_ms())
    async with withRecordTransaction("session", session_id, timeoutMs=timeout_ms):
        raw = await readJsonFileTolerant(sessionPath(session_id), None)
        if not isSessionHeadV2(raw):
            return None
        current_revision = normalizeSessionRevision(raw.get("revision"))
        body_ref = raw.get("bodyRef") if isinstance(raw.get("bodyRef"), dict) else {}
        raw_commit_id = body_ref.get("commitId")
        if (
            checkpoint.revision != current_revision
            or checkpoint.messageCount != raw.get("messageCount")
            or checkpoint.bodyCommitId != raw_commit_id
            or checkpoint.bodyCommitId is None
        ):
            return None
        latest_head = sessionLiveHeadFromV2(session_id, raw)
        result = await _await_mutation(mutation(copy.deepcopy(latest_head)))
        if not isinstance(result, dict) or "session" not in result or "changed" not in result:
            raise TypeError("Session head mutation must return {'session': ..., 'changed': bool}.")
        next_head = result["session"]
        if not isinstance(next_head, dict) or "messages" in next_head:
            raise TypeError("Session head mutation must return a head-only session record.")
        if next_head.get("id") != session_id:
            raise ValueError(
                f"Session head transaction cannot change identity ({session_id} -> {next_head.get('id')})."
            )
        changed = bool(result["changed"] or len(messages) != checkpoint.messageCount)
        if not changed:
            _set_session_persistence_checkpoint(
                {"messages": messages},
                current_revision,
                len(messages),
                checkpoint.bodyCommitId,
            )
            return {
                "changed": False,
                "fastPath": True,
                "revision": current_revision,
                "session": _materialize_live_session(latest_head, messages),
                "bodyCommitId": checkpoint.bodyCommitId,
            }
        committed_head = copy.deepcopy(next_head)
        committed_head["revision"] = current_revision + 1
        committed = await commitSessionV2Delta(
            session_id,
            raw,
            committed_head,
            messages,
            faultInjector=settings.get("faultInjector"),
        )
        safe_head = committed["head"]
        revision = normalizeSessionRevision(safe_head.get("revision"))
        body_commit_id = committed["bodyCommitId"]
        # Stamp the count the commit actually covered, not the live list's
        # current length: an append racing the commit stays unpersisted and
        # rides the next delta.
        _set_session_persistence_checkpoint(
            {"messages": messages}, revision, committed["messageCount"], body_commit_id
        )
        return {
            "changed": True,
            "fastPath": True,
            "revision": revision,
            "session": _materialize_live_session(safe_head, messages),
            "bodyCommitId": body_commit_id,
        }


def _materialize_live_session(
    head: dict[str, Any], messages: list[Any]
) -> dict[str, Any]:
    session = copy.deepcopy(head)
    session["messages"] = messages
    return session


async def _await_mutation(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


