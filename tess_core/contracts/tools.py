"""Tool result and payload manifest contracts (the model-facing surface).

Producers: every handler in ``tess_core/tools/*.py`` (result envelope),
``tess_core/payloads/tools.py`` (payload manifests and artifact references),
``tess_core/execution_descriptors.py`` (execution/activity descriptors
attached under ``meta.execution``).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class _tool_result_required(TypedDict):
    content: str


class ToolResult(_tool_result_required, total=False):
    """The envelope every tool handler returns to the agent loop."""

    isError: bool
    meta: dict[str, Any]               # carries "execution" descriptors
    pause: Literal["approval"]         # interactive approval pause
    structuredContent: Any             # bounded MCP structured companion
    attachments: list[dict[str, Any]]  # in-memory current-turn binary attachments


# Excerpt strategies emitted by the payload shapers.
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


class _tool_artifact_ref_required(TypedDict):
    artifactId: str
    toolName: str
    sessionId: str
    family: str
    filePath: str
    bytes: int
    lines: int
    createdAt: str


class ToolArtifactRef(_tool_artifact_ref_required, total=False):
    """Persisted artifact reference for large tool outputs (payloads/tools.py)."""

    taskId: str
    itemCount: int | None
    contentType: str


class _tool_payload_manifest_required(TypedDict):
    strategy: str
    truncated: bool
    originalBytes: int
    originalLines: int
    returnedBytes: int
    returnedLines: int


class ToolPayloadManifest(_tool_payload_manifest_required, total=False):
    """Manifest describing how a large tool output was excerpted."""

    artifact: ToolArtifactRef


class OutputManifest(TypedDict, total=False):
    """Task output manifest (execution_descriptors.summarize_output_manifest)."""

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


class _execution_descriptor_required(TypedDict):
    id: str
    kind: str        # "task" | "tool" | "workspace"
    title: str
    status: str


class ExecutionDescriptor(_execution_descriptor_required, total=False):
    """Execution descriptor attached under a tool result's ``meta.execution``."""

    cwd: str
    command: str
    startedAt: str
    finishedAt: str
    output: OutputManifest
    activities: list[ActivityDescriptor]
    metadata: dict[str, Any]
