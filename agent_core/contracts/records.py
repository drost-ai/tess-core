"""Persisted record contracts (cross-process / daemon-restart boundaries).

This module remains as the compatibility import surface. The concrete
definitions live in focused modules so each contract file stays small and
owned by one record family.
"""

from __future__ import annotations

from .monitor_records import MonitorRecord, MonitorStatus
from .session_records import (
    CompactionState,
    FullTranscriptLog,
    PersistedSessionHeadV2,
    SessionBodyCommitRecord,
    SessionBodyMessageRecord,
    SessionBodyRef,
    SessionBodyTruncateRecord,
    SessionMetadata,
    SessionRecord,
    SessionUsageTotals,
)
from .task_records import (
    ChildAgentRecord,
    ChildAgentStatus,
    ForegroundMode,
    HealthCheckStatus,
    OwnedProcessIdentity,
    SubagentCommunicationPolicy,
    SubagentRuntimeContract,
    SubagentStructuredResultStatus,
    TaskFamily,
    TaskKind,
    TaskLaunchResult,
    TaskLaunchReservation,
    TaskMeta,
    TaskMetaType,
    TaskOwner,
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
    "PersistedSessionHeadV2",
    "SessionBodyCommitRecord",
    "HealthCheckStatus",
    "OwnedProcessIdentity",
    "MonitorRecord",
    "MonitorStatus",
    "SessionMetadata",
    "SessionBodyMessageRecord",
    "SessionBodyRef",
    "SessionBodyTruncateRecord",
    "SessionRecord",
    "SubagentCommunicationPolicy",
    "SubagentRuntimeContract",
    "SubagentStructuredResultStatus",
    "SessionUsageTotals",
    "TaskFamily",
    "TaskKind",
    "TaskLaunchResult",
    "TaskLaunchReservation",
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
