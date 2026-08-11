from __future__ import annotations

from ..contracts.config import PERMISSION_MODES
from typing import Any

_OUTPUT_LIMIT_STOP_REASONS = {"max_tokens", "max_output_tokens", "length"}
_SUBAGENT_LIMIT_STOP_REASONS = {
    "max_turns",
    "subagent_max_turns_exceeded",
    "max_tool_calls",
    "subagent_max_tool_calls_exceeded",
    "timeout_ms",
}
_BUDGET_STOP_REASONS = {"max_budget_usd", "max_budget_exceeded"}


def sanitizePermissionMode(mode: Any) -> str:
    return mode if mode in PERMISSION_MODES else "default"


def inheritParentPermissionMode(childMode: str, parentMode: str) -> str:
    if parentMode == "bypassPermissions":
        return "bypassPermissions"
    if parentMode == "acceptEdits" and childMode == "default":
        return "acceptEdits"
    return childMode


def mapStopReasonToStatus(stopReason: str | None) -> str:
    if stopReason == "cancelled":
        return "cancelled"
    if stopReason == "no_progress":
        return "stopped"
    if stopReason in _OUTPUT_LIMIT_STOP_REASONS or stopReason in _SUBAGENT_LIMIT_STOP_REASONS:
        return "truncated"
    if stopReason in _BUDGET_STOP_REASONS:
        return "failed"
    return "completed"


def mapStopReasonToWaitingReason(stopReason: str | None) -> str | None:
    return None


def mapStopReasonToCompletionReason(stopReason: str | None) -> str | None:
    if stopReason in _OUTPUT_LIMIT_STOP_REASONS:
        return "max_output_tokens"
    if stopReason in {"max_turns", "subagent_max_turns_exceeded"}:
        return "max_turns"
    if stopReason in {"max_tool_calls", "subagent_max_tool_calls_exceeded"}:
        return "max_tool_calls"
    if stopReason == "timeout_ms":
        return "timeout_ms"
    if stopReason in _BUDGET_STOP_REASONS:
        return "max_budget_exceeded"
    return {
        "end_turn": "worker_completed",
        "cancelled": "cancelled",
        "no_progress": "no_progress",
    }.get(stopReason)
