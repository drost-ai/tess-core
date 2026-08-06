"""Typed contract boundaries for agent-core-python.

This package is the single place to look up the shape of every persisted and
cross-process contract: session/task/child-agent records, runtime event
envelopes, tool results, resolved config, and provider request/response
shapes. The TypedDicts describe the dicts the runtime already produces —
byte-for-byte, including legacy duplicate spellings — so adopting them never
changes a serialized record.

Rules:

- Contracts import nothing from runtime modules; runtime modules import from
  contracts.
- ``TypedDict`` everywhere a value is serialized; dataclasses only for pure
  in-memory values.
- The validator (``contracts.validate``) is a test/harness aid, never a
  production hot-path guard.

Type-check lane (dev/CI only, never a runtime dependency):

    python3 -m mypy --config-file pyproject.toml

The lane is enforced by ``tests/test_contracts_typecheck.py`` and skips
gracefully when mypy is not installed.
"""

from .config import (
    AgentDefinition,
    AutoModePolicy,
    OpenAiApiMode,
    PermissionMode,
    ProviderKind,
    ProviderProfile,
    ResolvedConfig,
    RuntimeSecretPurpose,
    RuntimeSecretRef,
    RuntimeSecretValue,
    SettingsRecord,
)
from .context import ToolContext
from .events import (
    ContextReliefMode,
    RuntimeEventEnvelope,
    RuntimeEventName,
    RuntimeSubscriberEnvelope,
    event_body,
    event_kind,
    event_name,
    event_payload,
    event_session_id,
    event_time,
)
from .providers import (
    NormalizedBlock,
    ProviderFailureCategory,
    ProviderFailureRecord,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
    TextBlock,
    ToolUseBlock,
)
from .records import (
    ChildAgentRecord,
    ChildAgentStatus,
    CompactionState,
    ForegroundMode,
    FullTranscriptLog,
    PersistedSessionHeadV2,
    SessionBodyCommitRecord,
    SessionBodyMessageRecord,
    SessionBodyRef,
    SessionBodyTruncateRecord,
    HealthCheckStatus,
    OwnedProcessIdentity,
    MonitorRecord,
    MonitorStatus,
    PlanApprovalRecord,
    PlanModeState,
    SessionMetadata,
    SessionRecord,
    SessionUsageTotals,
    TaskFamily,
    TaskKind,
    TaskLaunchResult,
    TaskLaunchReservation,
    TaskMeta,
    TaskMetaType,
    TaskOwner,
    TaskRecord,
    TaskStatus,
    launch_output_file,
    launch_task_id,
    task_meta_id,
)
from .tools import (
    ActivityDescriptor,
    ExecutionDescriptor,
    OutputManifest,
    PayloadStrategy,
    ToolArtifactRef,
    ToolPayloadManifest,
    ToolResult,
)
from .validate import assert_valid, validate

__all__ = [
    "ActivityDescriptor",
    "AgentDefinition",
    "AutoModePolicy",
    "ChildAgentRecord",
    "ChildAgentStatus",
    "CompactionState",
    "ContextReliefMode",
    "ExecutionDescriptor",
    "OwnedProcessIdentity",
    "ForegroundMode",
    "FullTranscriptLog",
    "PersistedSessionHeadV2",
    "SessionBodyCommitRecord",
    "HealthCheckStatus",
    "MonitorRecord",
    "MonitorStatus",
    "NormalizedBlock",
    "OpenAiApiMode",
    "OutputManifest",
    "PayloadStrategy",
    "PermissionMode",
    "PlanApprovalRecord",
    "PlanModeState",
    "ProviderFailureCategory",
    "ProviderFailureRecord",
    "ProviderKind",
    "ProviderProfile",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderUsage",
    "ResolvedConfig",
    "RuntimeSecretPurpose",
    "RuntimeSecretRef",
    "RuntimeSecretValue",
    "RuntimeEventEnvelope",
    "RuntimeEventName",
    "RuntimeSubscriberEnvelope",
    "SessionMetadata",
    "SessionBodyMessageRecord",
    "SessionBodyRef",
    "SessionBodyTruncateRecord",
    "SessionRecord",
    "SessionUsageTotals",
    "SettingsRecord",
    "TaskFamily",
    "TaskKind",
    "TaskLaunchResult",
    "TaskLaunchReservation",
    "TaskMeta",
    "TaskMetaType",
    "TaskOwner",
    "TaskRecord",
    "TaskStatus",
    "TextBlock",
    "ToolArtifactRef",
    "ToolContext",
    "ToolPayloadManifest",
    "ToolResult",
    "ToolUseBlock",
    "assert_valid",
    "event_body",
    "event_kind",
    "event_name",
    "event_payload",
    "event_session_id",
    "event_time",
    "launch_output_file",
    "launch_task_id",
    "task_meta_id",
    "validate",
]
