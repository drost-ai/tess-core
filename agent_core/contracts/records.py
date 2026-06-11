"""Persisted record contracts (cross-process / daemon-restart boundaries).

This module remains as the compatibility import surface. The concrete
definitions live in focused modules so each contract file stays small and
owned by one record family.
"""

from __future__ import annotations

from .monitor_records import MonitorRecord, MonitorStatus
from .plan_records import PlanApprovalRecord, PlanModeState
from .session_records import (
    CompactionState,
    FullTranscriptLog,
    SessionMetadata,
    SessionRecord,
    SessionUsageTotals,
)
from .task_records import (
    ChildAgentRecord,
    ChildAgentStatus,
    ForegroundMode,
    HealthCheckStatus,
    SubagentCommunicationPolicy,
    SubagentRuntimeContract,
    SubagentStructuredResultStatus,
    TaskEventRecord,
    TaskFamily,
    TaskKind,
    TaskLaunchResult,
    TaskMailboxMessage,
    TaskMeta,
    TaskMetaType,
    TaskOwner,
    TaskQuestionRecord,
    TaskQuestionStatus,
    TaskRecord,
    TaskStatus,
    launch_output_file,
    launch_task_id,
    task_meta_id,
)

__all__ = [
    "ChildAgentRecord",
    "ChildAgentStatus",
    "CompactionState",
    "ForegroundMode",
    "FullTranscriptLog",
    "HealthCheckStatus",
    "MonitorRecord",
    "MonitorStatus",
    "PlanApprovalRecord",
    "PlanModeState",
    "SessionMetadata",
    "SessionRecord",
    "SubagentCommunicationPolicy",
    "SubagentRuntimeContract",
    "SubagentStructuredResultStatus",
    "TaskEventRecord",
    "SessionUsageTotals",
    "TaskFamily",
    "TaskKind",
    "TaskLaunchResult",
    "TaskMailboxMessage",
    "TaskMeta",
    "TaskMetaType",
    "TaskOwner",
    "TaskQuestionRecord",
    "TaskQuestionStatus",
    "TaskRecord",
    "TaskStatus",
    "launch_output_file",
    "launch_task_id",
    "task_meta_id",
]
