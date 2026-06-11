"""Monitor persisted record contracts."""

from __future__ import annotations

from typing import Literal, TypedDict


MonitorStatus = Literal["active", "muted", "completed", "failed", "stopped"]


class _MonitorRecordRequired(TypedDict):
    id: str
    sessionId: str
    taskId: str
    createdAt: str
    updatedAt: str
    status: MonitorStatus
    goal: str
    checkIntervalMs: int
    heartbeatIntervalMs: int
    lastNarratedOutputSize: int
    lastObservedOutputSize: int


class MonitorRecord(_MonitorRecordRequired, total=False):
    """Monitor record at ``monitors/{monitorId}.json``."""

    summaryStyle: str | None
    muted: bool | None
    lastCheckAt: str | None
    lastObservedTaskStatus: str | None
    completedAt: str | None
    failureReason: str | None
    lastAssistantUpdateAt: str | None
    lastAssistantUpdateSummary: str | None
    lastHeartbeatAt: str | None
    stoppedAt: str | None


__all__ = ["MonitorRecord", "MonitorStatus"]
