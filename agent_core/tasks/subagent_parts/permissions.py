from __future__ import annotations

from .shared import *


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
    if stopReason == "max_tokens":
        return "truncated"
    if stopReason in {"max_budget_exceeded", "plan_rejected"}:
        return "failed"
    return "completed"


def mapStopReasonToWaitingReason(stopReason: str | None) -> str | None:
    if stopReason == "plan_approval_required":
        return "Awaiting plan approval before the worker can continue."
    if stopReason == "plan_chat_requested":
        return "Awaiting plan chat input before the worker can continue."
    return None


def mapStopReasonToCompletionReason(stopReason: str | None) -> str | None:
    return {
        "end_turn": "worker_completed",
        "cancelled": "cancelled",
        "max_tokens": "max_tokens",
        "max_budget_exceeded": "max_budget_exceeded",
        "plan_rejected": "plan_rejected",
    }.get(stopReason)
