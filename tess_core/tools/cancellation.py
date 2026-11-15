from __future__ import annotations

from .._utils import signal_aborted as _signal_aborted

from ..contracts.context import ToolContext

from typing import Any


def _throw_if_tool_cancelled(context: ToolContext) -> None:
    if _signal_aborted(context.get("abortSignal")):
        raise _create_tool_abort_error()


def _create_tool_abort_error() -> RuntimeError:
    error = RuntimeError("Tool execution cancelled")
    error.name = "AbortError"  # type: ignore[attr-defined]
    return error


