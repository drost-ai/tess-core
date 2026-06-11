"""Plan-mode persisted record contracts."""

from __future__ import annotations

from typing import Any, TypedDict


class _PlanModeStateRequired(TypedDict):
    state: str


class PlanModeState(_PlanModeStateRequired, total=False):
    """``metadata.planMode`` state."""

    requestId: str
    plan: str
    filePath: str
    allowedPrompts: list[dict[str, Any]]
    approvedAt: str
    rejectedAt: str
    reason: str


class _PlanApprovalRecordRequired(TypedDict):
    requestId: str


class PlanApprovalRecord(_PlanApprovalRecordRequired, total=False):
    """Plan approval record at ``plan-approvals/{requestId}.json``."""

    sessionId: str
    status: str
    plan: str
    filePath: str
    allowedPrompts: list[dict[str, Any]]
    cwd: str
    createdAt: str
    resolvedAt: str | None
    reason: str | None


__all__ = ["PlanApprovalRecord", "PlanModeState"]
