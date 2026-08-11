from __future__ import annotations

from .._utils import signal_aborted as _signal_aborted

from ..runtime.events.emit import safeEmit as _safe_emit

from dataclasses import dataclass as _dataclass

import copy
from typing import Any
from ..context.compaction_state import isCompactionStateNewer as _is_compaction_state_newer
from ..payloads.attachments import stripInMemoryImageData as _strip_in_memory_image_data
from ..storage.message_history import createSessionMessageList as _create_session_message_list


@_dataclass(frozen=True)
class _SessionRollbackRecord:
    messageWatermark: int
    messageSource: list[dict[str, Any]]
    frozenMessages: tuple[dict[str, Any], ...]
    head: dict[str, Any]


def _capture_session_rollback(
    session: dict[str, Any],
    previous: _SessionRollbackRecord | None = None,
) -> _SessionRollbackRecord:
    """Capture an owned rollback authority with incremental message copying.

    The first boundary of a turn owns a deep copy of the current history.
    Later append-only boundaries reuse that frozen prefix and copy only the
    newly appended suffix.  If the live list identity changes (for example,
    compaction), the record fails safe by taking a new owned copy.
    """

    messages = session.get("messages")
    if not isinstance(messages, list):
        raise TypeError("Session messages must be a list at a stable boundary.")
    can_extend = (
        previous is not None
        and previous.messageSource is messages
        and len(messages) >= previous.messageWatermark
    )
    frozen_messages = (
        previous.frozenMessages
        + tuple(copy.deepcopy(messages[previous.messageWatermark :]))
        if can_extend and previous is not None
        else tuple(copy.deepcopy(messages))
    )
    return _SessionRollbackRecord(
        messageWatermark=len(messages),
        messageSource=messages,
        frozenMessages=frozen_messages,
        head=copy.deepcopy({key: value for key, value in session.items() if key != "messages"}),
    )


def _materialize_rollback_snapshot(record: _SessionRollbackRecord) -> dict[str, Any]:
    if len(record.frozenMessages) != record.messageWatermark:
        raise RuntimeError("Rollback message authority does not match its stable watermark.")
    snapshot = copy.deepcopy(record.head)
    # Media payload removal stays rollback-equivalent to the legacy stable
    # snapshot, but its O(messages) copy is paid only on rollback, not on each
    # successful stable boundary.
    snapshot["messages"] = _create_session_message_list(
        _strip_in_memory_image_data(list(record.frozenMessages))
    )
    return snapshot


def _is_turn_cancelled(runtime: dict[str, Any]) -> bool:
    return _signal_aborted(runtime.get("abortSignal"))

def _throw_if_turn_cancelled(runtime: dict[str, Any]) -> None:
    signal = runtime.get("abortSignal")
    if not _signal_aborted(signal):
        return
    reason = _signal_reason(signal)
    if isinstance(reason, BaseException):
        raise reason
    error = RuntimeError(str(reason or "Turn cancelled"))
    error.name = "AbortError"  # type: ignore[attr-defined]
    raise error

def _is_turn_abort_error(error: BaseException, signal: Any = None) -> bool:
    if _signal_aborted(signal):
        return True
    if error.__class__.__name__ == "AbortError":
        return True
    return getattr(error, "name", None) == "AbortError"


def _signal_reason(signal: Any) -> Any:
    if not signal:
        return None
    if isinstance(signal, dict):
        return signal.get("reason")
    return getattr(signal, "reason", None)

def _restore_session_state(
    session: dict[str, Any],
    snapshot: dict[str, Any] | _SessionRollbackRecord,
) -> None:
    if isinstance(snapshot, _SessionRollbackRecord):
        snapshot = _materialize_rollback_snapshot(snapshot)
    preserved_permission_mode = session.get("permissionMode")
    live_metadata = session.get("metadata") or {}
    snapshot_metadata = snapshot.get("metadata") or {}
    live_compaction_state = live_metadata.get("compactionState")
    live_transcript_log = live_metadata.get("fullTranscriptLog")
    snapshot_transcript_log = snapshot_metadata.get("fullTranscriptLog")
    live_archive_count = (
        live_transcript_log.get("archivedMessageCount", 0)
        if isinstance(live_transcript_log, dict)
        else 0
    )
    snapshot_archive_count = (
        snapshot_transcript_log.get("archivedMessageCount", 0)
        if isinstance(snapshot_transcript_log, dict)
        else 0
    )
    newer_transcript_log = (
        copy.deepcopy(live_transcript_log)
        if live_archive_count > snapshot_archive_count
        else None
    )
    newer_compaction_state = (
        copy.deepcopy(live_compaction_state)
        if _is_compaction_state_newer(live_compaction_state, snapshot_metadata.get("compactionState"))
        else None
    )
    newer_compaction_count = None
    newer_runtime_restore = None
    if newer_compaction_state:
        newer_compaction_count = max(
            int(live_metadata.get("compactions") or 0),
            int(snapshot_metadata.get("compactions") or 0),
            int(newer_compaction_state.get("latestCompactionGeneration") or 0),
        )
        newer_runtime_restore = copy.deepcopy(live_metadata.get("runtimeRestore"))
    session.clear()
    session.update(copy.deepcopy(snapshot))
    if preserved_permission_mode is not None:
        session["permissionMode"] = preserved_permission_mode
    if newer_compaction_state:
        metadata = session.setdefault("metadata", {})
        metadata["compactionState"] = newer_compaction_state
        metadata["compactions"] = newer_compaction_count
        if newer_runtime_restore:
            metadata["runtimeRestore"] = newer_runtime_restore
    if newer_transcript_log:
        session.setdefault("metadata", {})["fullTranscriptLog"] = newer_transcript_log

def _now() -> str:
    from .._utils import now_iso

    return now_iso()
