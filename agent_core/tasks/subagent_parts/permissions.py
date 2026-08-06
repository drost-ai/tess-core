from __future__ import annotations

from .shared import PERMISSION_MODES
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
    if parentMode == "acceptEdits" and childMode in {"default", "plan"}:
        return "acceptEdits"
    if parentMode == "plan":
        return "plan"
    return childMode


def mapStopReasonToStatus(stopReason: str | None) -> str:
    if stopReason == "plan_approval_required":
        return "awaiting_plan_approval"
    if stopReason == "plan_chat_requested":
        return "awaiting_input"
    if stopReason == "cancelled":
        return "cancelled"
    if stopReason == "no_progress":
        return "stopped"
    if stopReason in _OUTPUT_LIMIT_STOP_REASONS or stopReason in _SUBAGENT_LIMIT_STOP_REASONS:
        return "truncated"
    if stopReason in _BUDGET_STOP_REASONS or stopReason == "plan_rejected":
        return "failed"
    return "completed"


def mapStopReasonToWaitingReason(stopReason: str | None) -> str | None:
    if stopReason == "plan_approval_required":
        return "Awaiting plan approval before the worker can continue."
    if stopReason == "plan_chat_requested":
        return "Awaiting plan chat input before the worker can continue."
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
        "plan_rejected": "plan_rejected",
    }.get(stopReason)


def parseAllowedPrompts(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict) and item.get("tool") == "Bash" and isinstance(item.get("prompt"), str) and item["prompt"].strip():
            out.append({"tool": "Bash", "prompt": item["prompt"].strip()})
    return out
