from __future__ import annotations

from typing import Any


def _throw_if_runtime_tool_cancelled(context: dict[str, Any]) -> None:
    signal = context.get("abortSignal") if isinstance(context, dict) else None
    if not _signal_aborted(signal):
        return
    reason = _signal_reason(signal)
    if isinstance(reason, BaseException):
        raise reason
    error = RuntimeError(str(reason or "MCP tool execution cancelled"))
    error.name = "AbortError"  # type: ignore[attr-defined]
    raise error


def _is_runtime_tool_abort_error(error: BaseException, signal: Any = None) -> bool:
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
