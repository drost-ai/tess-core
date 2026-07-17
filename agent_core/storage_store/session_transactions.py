from __future__ import annotations

import copy
from typing import Any, Callable

from ..runtime_identifiers import parseSessionId
from .shared import *


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
        return await _await_mutation(mutation(copy.deepcopy(latest)))

    return await mutateSessionRecordTransaction(sessionId, require_latest, options, **kwargs)


async def mutateSessionRecordTransaction(
    sessionId: str,
    mutation: Callable[[dict[str, Any] | None], Any],
    options: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    settings = {**(options or {}), **kwargs}
    timeout_ms = int(settings.get("timeoutMs", STORAGE_LOCK_TIMEOUT_MS))

    async def apply(latest: dict[str, Any] | None) -> dict[str, Any]:
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
            return {"value": latest, "changed": False}
        committed = copy.deepcopy(next_session)
        committed["revision"] = current_revision + 1
        return {
            "value": _redact_persisted_image_payloads(committed),
            "changed": True,
        }

    result = await mutateJsonRecord(
        namespace="session",
        key=session_id,
        filePath=sessionPath(session_id),
        fallback=None,
        mutate=apply,
        timeoutMs=timeout_ms,
    )
    session = result["value"]
    if session is None:
        raise SessionRecordNotFoundError(f"Session not found: {session_id}")
    return {
        "changed": result["changed"],
        "revision": normalizeSessionRevision(session.get("revision")),
        "session": session,
    }


async def _await_mutation(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


update_session_record = updateSessionRecord
mutate_session_record_transaction = mutateSessionRecordTransaction
normalize_session_revision = normalizeSessionRevision
