from __future__ import annotations

from .shared import *
from .._compaction_state import isCompactionStateNewer as _is_compaction_state_newer

def _safe_emit(runtime: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    try:
        runtime.get("emitEvent", lambda *_: None)(event, data)
    except Exception:
        pass

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

def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))

def _signal_reason(signal: Any) -> Any:
    if not signal:
        return None
    if isinstance(signal, dict):
        return signal.get("reason")
    return getattr(signal, "reason", None)

def _restore_session_state(session: dict[str, Any], snapshot: dict[str, Any]) -> None:
    preserved_permission_mode = session.get("permissionMode")
    live_metadata = session.get("metadata") or {}
    snapshot_metadata = snapshot.get("metadata") or {}
    live_compaction_state = live_metadata.get("compactionState")
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

def _now() -> str:
    from .._utils import now_iso

    return now_iso()
