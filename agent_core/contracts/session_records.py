"""Session persisted record contracts."""

from __future__ import annotations

from typing import Any, TypedDict

from .plan_records import PlanModeState


class SessionUsageTotals(TypedDict):
    """``metadata.usage`` counters."""

    turns: int
    toolCalls: int
    inputTokens: int
    outputTokens: int
    cacheCreationInputTokens: int
    cacheReadInputTokens: int
    cacheCreation5mInputTokens: int
    cacheCreation1hInputTokens: int
    cacheCreationTtlDetailComplete: bool


class CompactionState(TypedDict):
    """``metadata.compactionState`` checkpoint-v2 summary."""

    latestCompactionId: str
    latestCompactionGeneration: int
    latestCompactionAt: str
    compactedThroughMessageIndex: int
    compactedMessageCount: int
    recentMessageCount: int
    archiveMessageCount: int
    rawTokenEstimate: int
    compactedContextTokenEstimate: int
    workingContextTokenEstimate: int
    workingContextTokenMeasurement: dict[str, Any] | None
    lastTrigger: str
    qualityClassification: str
    compactionModel: str | dict[str, Any]
    verificationModel: str | dict[str, Any]
    verification: dict[str, Any]
    artifacts: dict[str, Any]


class FullTranscriptLog(TypedDict):
    """``metadata.fullTranscriptLog``."""

    path: str
    archivedMessageCount: int
    lastArchivedAt: str | None


class SessionMetadata(TypedDict, total=False):
    """``session["metadata"]``.

    Everything is optional because owning subsystems add and remove their own
    keys over the session lifecycle.
    """

    originalCwd: str
    assistantName: str | None
    assistantDescription: str | None
    usage: SessionUsageTotals
    compactions: int
    depth: int
    childSessionIds: list[str]
    childAgentIds: list[str]
    planMode: PlanModeState
    compactionState: CompactionState
    fullTranscriptLog: FullTranscriptLog
    forkedFromSessionId: str
    parentSessionId: str
    parentAgentId: str | None
    agentId: str | None
    agentName: str
    agentType: str | None
    teamName: str | None
    worktreePath: str | None
    backgroundSessionIds: list[str]
    taskIds: list[str]
    monitorIds: list[str]
    workspaceId: str | None
    workspaceName: str | None
    workspaceRoot: str | None
    workspaceMode: str | None
    gitRoot: str | None
    gitBranch: str | None
    prNumber: int | None
    prUrl: str | None
    tmuxMode: str | None
    tmuxSessionName: str | None
    tmuxWindowName: str | None
    tmuxUpdatedAt: str | None
    tmuxLastEvent: dict[str, Any] | None
    tmuxHandoffHandled: bool
    ideConnectionStatus: str | None
    ideName: str | None
    ideSource: str | None
    ideCommand: str | None
    ideConnectionReason: str | None
    ideTargetId: str | None
    ideUpdatedAt: str | None
    chromeIntegrationEnabled: bool
    chromeIntegrationMode: str | None
    chromePlatform: str | None
    chromeBrowserBinary: str | None
    chromeNativeHostManifestPath: str | None
    chromeNativeHostModeSupported: bool
    chromeUpdatedAt: str | None
    bridgeId: str | None
    remoteSessionId: str | None
    runtimeRestore: dict[str, Any] | None
    providerFailureCount: int
    lastProviderFailure: dict[str, Any]
    lastProviderRecoveryAt: str
    recovery_state: str


class SessionRecord(TypedDict):
    """Session record at ``sessions/{id}.json``."""

    id: str
    cwd: str
    createdAt: str
    updatedAt: str
    name: str | None
    provider: str
    providerProfileName: str | None
    model: str
    effort: str | None
    systemPrompt: str
    permissionMode: str
    messages: list[dict[str, Any]]
    todos: list[dict[str, Any]]
    metadata: SessionMetadata


__all__ = [
    "CompactionState",
    "FullTranscriptLog",
    "SessionMetadata",
    "SessionRecord",
    "SessionUsageTotals",
]
