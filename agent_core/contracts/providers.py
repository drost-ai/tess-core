"""Provider request/response contracts (the internal block model).

Producers: ``agent_core/providers/anthropic_parts/`` (request building, SSE
normalization), ``agent_core/providers/openai_parts/`` (params building,
stream normalization), ``agent_core/provider_errors.py`` (failure records).

Both providers normalize their streams into the same block model consumed by
the agent loop: ``{"content": [blocks], "usage": {...}, "stopReason": ...}``
with snake_case usage keys and camelCase ``stopReason``.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict, Union


class _ProviderRequestRequired(TypedDict):
    model: str
    messages: list[dict[str, Any]]


class ProviderRequest(_ProviderRequestRequired, total=False):
    """The normalized request dict passed to send*Request entry points."""

    system: str
    tools: list[dict[str, Any]]
    effort: str
    sessionId: str


class TextBlock(TypedDict):
    type: Literal["text"]
    text: str


class _ToolUseBlockRequired(TypedDict):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ToolUseBlock(_ToolUseBlockRequired, total=False):
    pass


NormalizedBlock = Union[TextBlock, ToolUseBlock]


class ProviderUsage(TypedDict, total=False):
    """Normalized usage (snake_case on both providers; cache keys Anthropic-only)."""

    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None


class _ProviderResponseRequired(TypedDict):
    content: list[dict[str, Any]]  # NormalizedBlock items
    usage: ProviderUsage
    model: str


class ProviderResponse(_ProviderResponseRequired, total=False):
    stopReason: str | None
    provider_metadata: dict[str, Any]  # OpenAI reasoning metadata


ProviderFailureCategory = Literal[
    "auth",
    "rate_limit",
    "quota",
    "timeout",
    "transport",
    "protocol",
    "server",
    "client_request",
    "cancelled",
    "unknown",
]


class ProviderFailureRecord(TypedDict):
    """createProviderFailureRecord output (provider_errors.py) — all keys present."""

    provider: str
    providerLabel: str
    phase: str
    category: ProviderFailureCategory
    message: str
    retryable: bool
    attempt: int
    hadAssistantOutput: bool
    hadToolUse: bool
    occurredAt: str
    status: int | None
    code: str | None
    errorType: str | None
    rawMessage: str | None
    rawBody: str | None
    streamEventType: str | None
