"""Tool result and payload manifest contracts (the model-facing surface).

Producers: every handler in ``agent_core/tools/*.py`` (result envelope),
``agent_core/tool_payloads.py`` (payload manifests and artifact references),
``agent_core/execution_descriptors.py`` (execution/activity descriptors
attached under ``meta.execution``).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class _ToolResultRequired(TypedDict):
    content: str


class ToolResult(_ToolResultRequired, total=False):
    """The envelope every tool handler returns to the agent loop."""

    isError: bool
    meta: dict[str, Any]               # carries "execution" descriptors
    pause: Literal["approval"]         # plan-mode approval pause
    structuredContent: Any             # bounded MCP structured companion
    attachments: list[dict[str, Any]]  # in-memory current-turn binary attachments


# Excerpt strategies emitted by tool_payloads.py shapers.
PayloadStrategy = Literal[
    "inline_full",
    "inline_head_tail",
    "inline_truncated_head",
    "inline_truncated_tail",
    "head",
    "tail",
    "head_tail",
    "count_and_sample",
    "windowed_file_read",
]


class _ToolArtifactRefRequired(TypedDict):
    artifactId: str
    toolName: str
    sessionId: str
    family: str
    filePath: str
    bytes: int
    lines: int
    createdAt: str


class ToolArtifactRef(_ToolArtifactRefRequired, total=False):
    """Persisted artifact reference for large tool outputs (tool_payloads.py)."""

    taskId: str
    itemCount: int | None
    contentType: str


class _ToolPayloadManifestRequired(TypedDict):
    strategy: str
    truncated: bool
    originalBytes: int
    originalLines: int
    returnedBytes: int
    returnedLines: int


class ToolPayloadManifest(_ToolPayloadManifestRequired, total=False):
    """Manifest describing how a large tool output was excerpted."""

    artifact: ToolArtifactRef


class OutputManifest(TypedDict, total=False):
    """Task output manifest (execution_descriptors.summarizeOutputManifest)."""

    filePath: str
    bytes: int
    lineCount: int
    truncated: bool
    excerpt: str


class ActivityDescriptor(TypedDict, total=False):
    """Activity entry attached to execution descriptors (plugin/MCP/workspace)."""

    kind: str
    status: str
    summary: str
    subjectId: str
    metadata: dict[str, Any]


class _ExecutionDescriptorRequired(TypedDict):
    id: str
    kind: str        # "task" | "tool" | "workspace"
    title: str
    status: str


class ExecutionDescriptor(_ExecutionDescriptorRequired, total=False):
    """Execution descriptor attached under a tool result's ``meta.execution``."""

    cwd: str
    command: str
    startedAt: str
    finishedAt: str
    output: OutputManifest
    activities: list[ActivityDescriptor]
    metadata: dict[str, Any]
