from __future__ import annotations

from .shared import *

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
    session.clear()
    session.update(copy.deepcopy(snapshot))
    if preserved_permission_mode is not None:
        session["permissionMode"] = preserved_permission_mode

def _now() -> str:
    from .._utils import now_iso

    return now_iso()
