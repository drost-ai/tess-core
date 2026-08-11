"""Provider request/response contracts (the internal block model).

Producers: ``agent_core/providers/anthropic/`` (request building, SSE
normalization), ``agent_core/providers/openai/`` (params building,
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
    promptCache: dict[str, Any]


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

# Anthropic content blocks round-trip verbatim (thinking, redacted_thinking,
# and server-tool blocks included since Epic G) — the open dict alias is the
# truthful type until the payload plane narrows it.
AnthropicBlock = dict[str, Any]


class AnthropicMessage(TypedDict):
    """One conversation message in Anthropic wire shape."""

    role: str
    content: str | list[AnthropicBlock]


class ProviderUsage(TypedDict, total=False):
    """Normalized usage (snake_case on both providers; cache keys Anthropic-only)."""

    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_creation_5m_input_tokens: int | None
    cache_creation_1h_input_tokens: int | None
    cache_creation_ttl_detail_complete: bool
    cache_read_input_tokens: int | None


# Anthropic usage arrives in this same normalized shape (the cache keys are
# Anthropic-only); the alias is the contract name shared with other runtimes.
AnthropicUsage = ProviderUsage


class _ProviderResponseRequired(TypedDict):
    content: list[dict[str, Any]]  # NormalizedBlock items
    usage: ProviderUsage
    model: str


class ProviderResponse(_ProviderResponseRequired, total=False):
    stopReason: str | None
    provider_metadata: dict[str, Any]  # OpenAI reasoning metadata
    promptCache: dict[str, Any]  # Internal cache observation; never serialized to provider.


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


class _ProviderFailureRequired(TypedDict):
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


class ProviderFailureRecord(_ProviderFailureRequired, total=False):
    """Bounded failure evidence; recovery lineage fields are present when known."""

    requestId: str | None
    logicalCallId: str | None
    generation: int | None
    partial_output_mode: Literal["buffered", "replaceable", "append_only"] | None
    responseId: str | None
    sequenceNumber: int | None
    recoveryFailure: str | None
    interruptedPreview: dict[str, Any] | None
    retryAfterMs: int | None
