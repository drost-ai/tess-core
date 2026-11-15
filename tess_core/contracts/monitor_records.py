"""Monitor persisted record contracts."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


MonitorStatus = Literal["active", "muted", "completed", "failed", "stopped"]


class _monitor_record_required(TypedDict):
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


class MonitorRecord(_monitor_record_required, total=False):
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
    deliveryStoppedAt: str
    delivery: dict[str, Any]


__all__ = ["MonitorRecord", "MonitorStatus"]
