"""Runtime event envelope contract.

Producer: ``agent_core/runtime_event_parts/factory.py`` (``createRuntimeEvent``).
Consumers: ``runtime_state_parts/reducer.py``, collaboration/monitor runtimes,
daemon replay (``runtime.py``), shell transcript rendering.

The envelope deliberately carries every historical spelling so all readers are
satisfied: the *raw* view (``event`` + ``data``) and the *typed* projection
(``kind`` + ``payload``), plus duplicated timestamps and legacy aliases. The
TypedDict models that reality exactly — no wire change. New readers should use
the canonical accessors below instead of inlining ``or``-chains; the accessors
are what make a future de-duplication of the envelope possible.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, TypedDict


# Mirrors KNOWN_RUNTIME_EVENTS in runtime_event_parts/shared.py; a sync test
# asserts the two sets are identical. The envelope's name fields stay ``str``
# because createRuntimeEvent accepts arbitrary event names.
RuntimeEventName = Literal[
    "system.init",
    "session.activated",
    "message.user",
    "message.assistant",
    "message.assistant.partial",
    "message.assistant.thinking.partial",
    "transcript.repaired",
    "turn.started",
    "turn.cancelling",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "status.updated",
    "status.cleared",
    "provider.retrying",
    "provider.recovered",
    "provider.consumer_sink_degraded",
    "provider.attempt_superseding",
    "provider.attempt_superseded",
    "provider.failed",
    "provider.activity",
    "provider.input_tokens",
    "provider.response_completed",
    "provider.prompt_cache",
    "provider.fallback",
    "tool.started",
    "tool.completed",
    "context.raw_pressure",
    "compaction.decision",
    "compaction.headroom",
    "permission.requested",
    "permission.decided",
    "sandbox.applied",
    "sandbox.downgraded",
    "plan.requested",
    "plan.approved",
    "plan.rejected",
    "plan.contract_enforced",
    "plan.mcp_tool_allowed",
    "plan.mcp_tool_blocked",
    "mcp.authorization_required",
    "mcp.catalog_changed_applied",
    "mcp.catalog_refresh_failed",
    "mcp.server_connected",
    "mcp.server_connection_failed",
    "compaction.started",
    "compaction.skipped",
    "compaction.completed",
    "compaction.failed",
    "context_relief.selected",
    "context_relief.applied",
    "daemon.snapshot",
    "daemon.recovering",
    "daemon.recovered",
    "task.snapshot",
    "task.registered",
    "task.updated",
    "task.output_delta",
    "task.completed",
    "task.failed",
    "task.restored",
    "subagent.background.queued",
    "subagent.background.running",
    "subagent.background.completed",
    "subagent.background.failed",
    "subagent.background.stopped",
    "subagent.background.timed_out",
    "subagent.reconciliation.created",
    "subagent.reconciliation.appended",
    "subagent.reconciliation.consumed",
    "subagent.reconciliation.skipped",
    "subagent.continuation.requested",
    "subagent.continuation.started",
    "subagent.continuation.completed",
    "subagent.continuation.failed",
    "subagent.depth_blocked",
    "health_check.unhealthy",
    "health_check.recovered",
    "monitor.snapshot",
    "monitor.created",
    "monitor.assistant_update",
    "monitor.status_changed",
    "collaboration.snapshot",
    "collaboration.assistant_update",
    "remote.status_changed",
    "bridge.status_changed",
    "bridge.message",
    "storage.lock_timeout",
    "result",
]

# Mirrors _CONTEXT_RELIEF_MODES in runtime_event_parts/shared.py (sync-tested).
ContextReliefMode = Literal[
    "microcompact",
    "api_microcompact",
    "partial_compaction",
    "session_memory_compaction",
    "full_checkpoint_compaction",
]


class RuntimeEventEnvelope(TypedDict):
    """The exact shape emitted by createRuntimeEvent — every key always present."""

    event: str | None       # canonical raw event name
    type: str | None        # duplicate of event (legacy)
    kind: str                # typed payload kind ("" when the event is unknown)
    data: dict[str, Any]     # raw event body as supplied by the emitter
    payload: dict[str, Any]  # typed camelCase projection of data
    createdAt: str           # canonical timestamp (ISO 8601)
    timestamp: str           # duplicate of createdAt (legacy)
    sessionId: str | None
    legacyEvent: str | None  # duplicate of event (legacy)
    legacyData: dict[str, Any]  # duplicate of data (legacy)


class RuntimeSubscriberEnvelope(TypedDict):
    """What ``bootstrapRuntime``'s ``subscribeEvents`` subscribers receive.

    The full typed envelope is nested under ``typedEvent``; ``event``/``data``
    repeat the raw name and body for legacy subscribers (runtime.py
    ``emit_runtime_event``).
    """

    event: str
    data: dict[str, Any]
    typedEvent: RuntimeEventEnvelope


class ProviderPromptCachePayload(TypedDict, total=False):
    phase: Literal["observed", "state_error", "eviction_intent"]
    mode: Literal["off", "explicit", "auto"] | None
    ttl: Literal["provider_default", "5m", "1h", "24h"] | None
    scope: Literal["ephemeral", "global", "bedrock"] | None
    usedBreakpoints: int | None
    placements: list[Literal["automatic", "system", "tools", "tail_a", "tail_b"]] | None
    cacheReadInputTokens: int | None
    cacheCreationInputTokens: int | None
    cacheCreation5mInputTokens: int | None
    cacheCreation1hInputTokens: int | None
    cacheCreationTtlDetailComplete: bool | None
    localReason: str | None
    providerReason: str | None
    stateStatus: Literal["committed", "stale", "unavailable", "recorded"] | None
    intentStatus: Literal["recorded"] | None


def event_kind(event: Mapping[str, Any]) -> str:
    """Typed event kind, preferring the typed projection.

    Mirrors the reducer's historical read order (kind, type, event) exactly.
    """
    value = event.get("kind") or event.get("type") or event.get("event")
    return value if isinstance(value, str) else ""


def event_name(event: Mapping[str, Any]) -> str:
    """Raw event name, preferring the raw spelling.

    Mirrors createRuntimeEvent's input coalescing (event, type, kind) exactly.
    """
    value = event.get("event") or event.get("type") or event.get("kind")
    return value if isinstance(value, str) else ""


def event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Typed event body, preferring the typed projection (payload, then data)."""
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def event_body(event: Mapping[str, Any]) -> dict[str, Any]:
    """Raw event body, preferring the raw spelling (data, then payload)."""
    data = event.get("data")
    if isinstance(data, dict):
        return data
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def event_time(event: Mapping[str, Any]) -> str:
    """Event timestamp (timestamp, then createdAt); empty string when absent."""
    value = event.get("timestamp") or event.get("createdAt")
    return value if isinstance(value, str) else ""


def event_session_id(event: Mapping[str, Any]) -> str | None:
    """Session id from the envelope, falling back to the body's spellings."""
    value = event.get("sessionId")
    if isinstance(value, str) and value:
        return value
    body = event_body(event)
    for key in ("session_id", "sessionId"):
        candidate = body.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None
