"""Persisted record contracts (cross-process / daemon-restart boundaries).

Producers: ``agent_core/storage_store/records.py`` (write/read helpers),
``agent_core/tasks/supervisor.py`` and ``tasks/supervisor_parts/records.py``
(task meta + task record projections), ``tasks/subagent_parts/lifecycle.py``
and finalizers (child-agent records), ``agent_core/monitors.py`` (monitor
records), ``tasks/supervisor_parts/health.py`` (health-check records).

These TypedDicts model the records as they exist on disk today, including
legacy duplicate spellings (``task_id`` alongside ``taskId``). Required keys
are limited to what every current writer is structurally guaranteed to emit;
``saveChildAgent`` drops ``None``-valued keys on write, so anything that can
be ``None`` at construction time is optional here.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


# Mirrors _TASK_STATUSES in runtime_event_parts/shared.py (sync-tested).
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

# Mirrors _TASK_KINDS in runtime_event_parts/shared.py (sync-tested).
TaskKind = Literal[
    "background_command",
    "monitor",
    "health_check",
    "main_session",
    "subagent",
    "remote",
]

# The `type` discriminator written into task meta by the supervisor launchers.
TaskMetaType = Literal["background_command", "main_session", "subagent", "health_check"]

# The `taskFamily` projection in _build_task_record and the family builders.
TaskFamily = Literal["shell", "subagent", "health_check", "detached_session", "monitor", "remote"]

ForegroundMode = Literal["foreground", "background"]

TaskOwner = Literal["daemon", "client"]

# Child-agent lifecycle statuses written by subagent lifecycle/finalizers.
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
    "awaiting_plan_approval",
]

MonitorStatus = Literal["active", "muted", "completed", "failed", "stopped"]

HealthCheckStatus = Literal["active", "unhealthy", "failed", "stopped"]


class _TaskLaunchResultRequired(TypedDict):
    taskId: str   # canonical
    task_id: str  # DEPRECATED duplicate; every launcher emits both spellings


class TaskLaunchResult(_TaskLaunchResultRequired, total=False):
    """Return value of the supervisor's start* launchers (tasks/supervisor.py).

    ``outputFile``/``output_file`` are emitted by every launcher except
    ``startHealthCheck``.
    """

    outputFile: str
    output_file: str  # DEPRECATED duplicate of outputFile
    pid: int


class _TaskMetaRequired(TypedDict):
    type: TaskMetaType
    status: str          # supervisor lifecycle status; superset of TaskStatus
    startedAt: str | None
    outputFile: str


class TaskMeta(_TaskMetaRequired, total=False):
    """Task meta as persisted at ``tasks/{taskId}/meta.json`` (writeTaskMeta).

    The subagent finalizer (tasks/subagent_parts/finalizers.py) writes only the
    snake_case ``task_id`` spelling, which is why the id fields are optional
    and dual-spelled.
    """

    taskId: str          # canonical id (supervisor launchers)
    task_id: str         # DEPRECATED duplicate (subagent finalizer writes only this)
    id: str              # duplicate of taskId (supervisor launchers)
    sessionId: str | None
    session_id: str      # DEPRECATED duplicate (subagent meta)
    agent_id: str        # subagent meta only
    parent_session_id: str | None  # subagent meta only
    parent_task_id: str | None     # subagent meta only
    command: str | None
    url: str | None      # health-check http mode
    mode: str            # health-check: "command" | "http"
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
    # --- health-check meta (start_health_check writes the meta and the
    # health-check record with the same shape) ---
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


class _TaskRecordRequired(TypedDict):
    id: str
    sessionId: str | None
    sourceId: str | None
    kind: TaskKind
    status: TaskStatus
    title: str | None
    summary: str | None
    createdAt: str | None
    updatedAt: str | None
    startedAt: str | None
    completedAt: str | None
    foregroundable: bool
    restorable: bool
    taskFamily: TaskFamily
    detailStatus: str | None
    retrieveOnly: bool
    resultSummary: str | None
    metadata: dict[str, Any]


class TaskRecord(_TaskRecordRequired, total=False):
    """Task record projection at ``task-records/{taskId}.json`` (saveTaskRecord).

    Built by ``_build_task_record`` and the monitor/remote/child-agent record
    builders in tasks/supervisor_parts/records.py. The optional keys below are
    family-specific: monitor records omit command/cwd/outputFile, remote
    records omit outputCursor, and so on.
    """

    cwd: str | None
    command: str | None
    outputFile: str | None
    outputCursor: int | None
    relatedMonitorIds: list[str]
    foregroundMode: ForegroundMode
    attachableSessionId: str | None
    parentTaskId: str | None
    relatedChildSessionIds: list[str]
    owner: TaskOwner | None
    supervisorId: str | None


class _ChildAgentRecordRequired(TypedDict):
    agentId: str
    sessionId: str
    parentSessionId: str
    name: str
    description: str
    status: ChildAgentStatus
    startedAt: str


class ChildAgentRecord(_ChildAgentRecordRequired, total=False):
    """Child-agent record at ``agents/{agentId}.json`` (saveChildAgent).

    ``saveChildAgent`` strips ``None``-valued keys on write, so every field
    that can be ``None`` at construction time is genuinely absent on disk.
    """

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
    """Monitor record at ``monitors/{monitorId}.json`` (agent_core/monitors.py)."""

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


class SessionUsageTotals(TypedDict):
    """metadata.usage counters (storage_store/sessions.py createEmptySession)."""

    turns: int
    toolCalls: int
    inputTokens: int
    outputTokens: int
    cacheCreationInputTokens: int
    cacheReadInputTokens: int


class _PlanModeStateRequired(TypedDict):
    state: str  # "inactive" | "pending" | "approved" | "chatting" | "rejected"


class PlanModeState(_PlanModeStateRequired, total=False):
    """metadata.planMode (tools/session_tools.py ExitPlanMode)."""

    requestId: str
    plan: str
    filePath: str
    allowedPrompts: list[dict[str, Any]]
    approvedAt: str
    rejectedAt: str
    reason: str


class CompactionState(TypedDict):
    """metadata.compactionState (context/compaction/orchestration.py) — all keys written."""

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
    compactionModel: str
    verificationModel: str
    verification: dict[str, Any]
    artifacts: dict[str, Any]


class FullTranscriptLog(TypedDict):
    """metadata.fullTranscriptLog (storage_store/sessions.py loadSession/persist)."""

    path: str
    archivedMessageCount: int
    lastArchivedAt: str | None


class SessionMetadata(TypedDict, total=False):
    """session["metadata"] — created by createEmptySession, extended by many writers.

    Everything is optional: subagent preparation pops keys (originalCwd,
    assistantName, assistantDescription) and most keys are written only by
    their owning subsystem (workspace, tmux/IDE/Chrome integrations, remote
    bridges, compaction, supervision).
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
    recovery_state: str  # DEPRECATED snake_case (supervisor reconciliation)


class SessionRecord(TypedDict):
    """Session record at ``sessions/{id}.json`` — every key set by createEmptySession.

    Top-level keys are never removed by later writers (metadata keys can be);
    several carry ``None`` values rather than being omitted.
    """

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
