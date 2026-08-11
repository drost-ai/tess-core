"""Task and child-agent persisted record contracts."""

from __future__ import annotations

from typing import Any, Literal, Mapping, TypedDict


# Mirrors _TASK_STATUSES in runtime/events/shared.py (sync-tested).
TaskStatus = Literal[
    "queued",
    "running",
    "backgrounded",
    "awaiting_input",
    "reconciling",
    "degraded",
    "completed",
    "failed",
    "stopped",
    "idle",
]

# Mirrors _TASK_KINDS in runtime/events/shared.py (sync-tested).
TaskKind = Literal[
    "background_command",
    "monitor",
    "health_check",
    "main_session",
    "subagent",
    "remote",
]

TaskMetaType = Literal["background_command", "main_session", "subagent", "health_check"]
TaskFamily = Literal["shell", "subagent", "health_check", "detached_session", "monitor", "remote"]
ForegroundMode = Literal["foreground", "background"]
TaskOwner = Literal["daemon", "client"]
ChildAgentStatus = Literal[
    "queued",
    "running",
    "backgrounded",
    "completed",
    "failed",
    "cancelled",
    "stopped",
    "truncated",
    "awaiting_input",
]
HealthCheckStatus = Literal[
    "active",
    "unhealthy",
    "failed",
    "stopped",
    "suspended_unverified",
    "suspended_for_rollback",
]
TaskQuestionStatus = Literal["pending", "answered", "skipped"]
SubagentStructuredResultStatus = Literal["not_configured", "valid", "invalid"]


class OwnedProcessIdentity(TypedDict):
    schemaVersion: Literal[1]
    pid: int
    processGroupId: int
    spawnedAt: str
    startToken: str
    observedCommandDigest: str
    expectedCommandDigest: str
    platform: str


class TaskLaunchReservation(TypedDict, total=False):
    schemaVersion: Literal[1]
    reservationId: str
    taskId: str
    sessionId: str
    kind: Literal["background_command", "main_session", "subagent"]
    state: Literal["starting", "attached", "failed"]
    createdAt: str
    attachedAt: str
    failedAt: str
    failureReason: str


class SubagentCommunicationPolicy(TypedDict, total=False):
    allowChildQuestions: bool
    maxPendingQuestions: int


class SubagentRuntimeContract(TypedDict, total=False):
    tools: list[str]
    maxBudgetUsd: float
    maxTurns: int
    timeoutMs: int
    maxToolCalls: int
    responseSchema: dict[str, Any]
    communication: SubagentCommunicationPolicy
    concurrencyGroup: str


class _TaskLaunchResultRequired(TypedDict):
    taskId: str
    task_id: str


class TaskLaunchResult(_TaskLaunchResultRequired, total=False):
    """Return value of the supervisor's start* launchers."""

    outputFile: str
    output_file: str
    pid: int


def launch_task_id(result: Mapping[str, Any]) -> str | None:
    """Canonical task id from a TaskLaunchResult."""
    return result.get("taskId") or result.get("task_id") or None


def launch_output_file(result: Mapping[str, Any]) -> str | None:
    """Canonical output file from a TaskLaunchResult."""
    return result.get("outputFile") or result.get("output_file") or None


def task_meta_id(meta: Mapping[str, Any]) -> str | None:
    """Canonical task id from TaskMeta."""
    return meta.get("taskId") or meta.get("task_id") or meta.get("id") or None


class _TaskMetaRequired(TypedDict):
    type: TaskMetaType
    status: str
    startedAt: str | None
    outputFile: str


class TaskMeta(_TaskMetaRequired, total=False):
    """Task meta as persisted at ``tasks/{taskId}/meta.json``."""

    taskId: str
    task_id: str
    id: str
    sessionId: str | None
    session_id: str
    agent_id: str
    parent_session_id: str | None
    parent_task_id: str | None
    runtimeContract: SubagentRuntimeContract | None
    concurrencyGroup: str | None
    command: str | None
    url: str | None
    mode: str
    cwd: str | None
    createdAt: str
    updatedAt: str
    finishedAt: str
    stoppedAt: str
    completedAt: str | None
    title: str | None
    description: str | None
    pid: int | None
    runnerPid: int | None
    detailStatus: str | None
    owner: TaskOwner
    supervisorId: str | None
    exitCode: int | None
    signal: str | None
    error: str | None
    attachableSessionId: str | None
    retryable: bool
    stoppable: bool
    stopReason: str | None
    summary: str | None
    resultSummary: str | None
    waitingReason: str | None
    completionReason: str | None
    processState: str | None
    processReconciledAt: str | None
    metadata: dict[str, Any]
    retryOfTaskId: str | None
    retriedAsTaskId: str | None
    intervalMs: int
    currentIntervalMs: int
    timeoutMs: int
    backoffMultiplier: float
    maxIntervalMs: int
    failureThreshold: int
    expectedStatusCodes: list[int] | None
    expectedBodySubstring: str | None
    lastRunAt: str | None
    lastResult: dict[str, Any] | None
    consecutiveFailures: int
    lastSuccessAt: str | None
    lastFailureAt: str | None
    lastStatusCode: int | None
    lastExitCode: int | None
    lastSignal: str | None
    lastTimedOut: bool
    lastTerminated: bool
    lastError: str | None
    lastResponseSnippet: str | None
    structuredResultStatus: SubagentStructuredResultStatus
    structuredResultPath: str
    structuredResultErrors: list[str]
    processIdentity: OwnedProcessIdentity
    launchReservation: TaskLaunchReservation


class _TaskRecordRequired(TypedDict):
    id: str
    sessionId: str | None
    sourceId: str | None
    kind: TaskKind
    status: TaskStatus
    title: str | None
    createdAt: str | None
    updatedAt: str | None
    startedAt: str | None
    foregroundable: bool
    restorable: bool


class TaskRecord(_TaskRecordRequired, total=False):
    """Task record projection at ``task-records/{taskId}.json``."""

    summary: str | None
    completedAt: str | None
    cwd: str | None
    command: str | None
    outputFile: str | None
    outputCursor: int | None
    relatedMonitorIds: list[str]
    foregroundMode: ForegroundMode
    attachableSessionId: str | None
    parentTaskId: str | None
    relatedChildSessionIds: list[str]
    taskFamily: TaskFamily
    detailStatus: str | None
    retrieveOnly: bool
    resultSummary: str | None
    metadata: dict[str, Any]
    owner: TaskOwner | None
    supervisorId: str | None
    processIdentity: OwnedProcessIdentity
    launchReservation: TaskLaunchReservation


class _ChildAgentRecordRequired(TypedDict):
    agentId: str
    sessionId: str
    parentSessionId: str
    name: str
    description: str
    status: ChildAgentStatus
    startedAt: str


class ChildAgentRecord(_ChildAgentRecordRequired, total=False):
    """Child-agent record at ``agents/{agentId}.json``."""

    parentToolUseId: str
    parentTaskId: str
    agentType: str
    teamName: str
    provider: str
    model: str
    mode: str
    isolation: Literal["worktree"]
    worktreePath: str
    runInBackground: bool
    completedAt: str
    owner: TaskOwner
    attachableSessionId: str
    taskId: str
    pid: int
    waitingReason: str
    completionReason: str
    resultSummary: str
    latestResultAt: str
    retryable: bool
    stoppable: bool
    recoveryState: str
    lastUpdatedAt: str
    lastSummary: str
    stopReason: str
    error: str
    runtimeContract: SubagentRuntimeContract | None
    structuredResultStatus: SubagentStructuredResultStatus
    structuredResult: dict[str, Any] | list[Any] | str | int | float | bool | None
    structuredResultPath: str
    structuredResultErrors: list[str]
    processIdentity: OwnedProcessIdentity


__all__ = [
    "ChildAgentRecord",
    "ChildAgentStatus",
    "ForegroundMode",
    "HealthCheckStatus",
    "OwnedProcessIdentity",
    "TaskFamily",
    "TaskKind",
    "TaskLaunchResult",
    "TaskLaunchReservation",
    "SubagentCommunicationPolicy",
    "SubagentRuntimeContract",
    "SubagentStructuredResultStatus",
    "TaskMeta",
    "TaskMetaType",
    "TaskOwner",
    "TaskQuestionStatus",
    "TaskRecord",
    "TaskStatus",
    "launch_output_file",
    "launch_task_id",
    "task_meta_id",
]
