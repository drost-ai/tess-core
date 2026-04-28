from __future__ import annotations

from typing import Any


def _throw_if_tool_cancelled(context: dict[str, Any]) -> None:
    if _signal_aborted(context.get("abortSignal")):
        raise _create_tool_abort_error()


def _create_tool_abort_error() -> RuntimeError:
    error = RuntimeError("Tool execution cancelled")
    error.name = "AbortError"  # type: ignore[attr-defined]
    return error


def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))
