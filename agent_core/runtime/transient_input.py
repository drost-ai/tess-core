from __future__ import annotations

import copy
from typing import Any

from ..payloads.media_persistence import sanitizeMediaForPersistence
from ..session.message_origin import isGenuineUserRequest as _is_genuine_user_request


def createTransientExecutionSession(
    session: dict[str, Any],
    overlay: Any,
) -> dict[str, Any]:
    """Apply one current-turn provider message to an execution-only copy."""

    if overlay is None:
        return session
    if not isinstance(overlay, dict):
        raise TypeError("transientInputOverlay must be an object.")
    message = overlay.get("message")
    if not _is_genuine_user_request(message):
        raise ValueError("transientInputOverlay.message must be a user message.")
    session_messages = session.get("messages")
    if not isinstance(session_messages, list):
        raise ValueError("transientInputOverlay requires canonical session messages.")
    target_index = _transient_overlay_target_index(session_messages, overlay)
    if target_index < 0 or target_index >= len(session_messages):
        raise ValueError("transientInputOverlay target is outside canonical history.")
    canonical_message = session_messages[target_index]
    if (
        not _is_genuine_user_request(canonical_message)
    ):
        raise ValueError(
            "transientInputOverlay target must be a canonical user message."
        )
    execution = copy.deepcopy(session)
    provider_message = copy.deepcopy(message)
    if "metadata" in canonical_message:
        provider_message["metadata"] = copy.deepcopy(canonical_message["metadata"])
    execution["messages"][target_index] = provider_message
    return execution


def syncTransientExecutionSession(
    canonical_session: dict[str, Any],
    execution_session: dict[str, Any],
) -> None:
    canonical_session.clear()
    canonical_session.update(sanitizeMediaForPersistence(execution_session))


def runtimeWithSanitizedEvents(runtime: dict[str, Any]) -> dict[str, Any]:
    emit = runtime.get("emitEvent")
    if not callable(emit):
        return runtime
    sanitized = dict(runtime)

    def emit_sanitized(event: str, data: Any = None) -> Any:
        return emit(
            event,
            sanitizeMediaForPersistence(data if data is not None else {}),
        )

    sanitized["emitEvent"] = emit_sanitized
    return sanitized


def _transient_overlay_target_index(
    messages: list[Any],
    overlay: dict[str, Any],
) -> int:
    turn_id = str(overlay.get("turnId") or "").strip()
    if turn_id:
        matches = [
            index
            for index, message in enumerate(messages)
            if _host_turn_id(message) == turn_id
        ]
        if len(matches) != 1:
            raise ValueError(
                "transientInputOverlay.turnId must identify exactly one canonical message."
            )
        return matches[0]
    index = overlay.get("messageIndex")
    if isinstance(index, bool) or not isinstance(index, int):
        raise ValueError(
            "transientInputOverlay requires a turnId or integer messageIndex."
        )
    return index


def _host_turn_id(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    metadata = message.get("metadata")
    host = metadata.get("host") if isinstance(metadata, dict) else None
    if not isinstance(host, dict):
        return ""
    return str(host.get("turn_id") or host.get("turnId") or "").strip()


create_transient_execution_session = createTransientExecutionSession
sync_transient_execution_session = syncTransientExecutionSession
runtime_with_sanitized_events = runtimeWithSanitizedEvents
