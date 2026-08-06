from __future__ import annotations

import math
from typing import Any


_PHASES = {"observed", "state_error", "eviction_intent"}
_MODES = {"off", "explicit", "auto"}
_TTLS = {"provider_default", "5m", "1h", "24h"}
_SCOPES = {"ephemeral", "global", "bedrock"}
_PLACEMENTS = {"automatic", "system", "tools", "tail_a", "tail_b"}
_LOCAL_REASONS = {
    "warmup_no_previous_state", "state_schema_changed", "provider_route_changed", "model_changed",
    "cache_policy_changed", "beta_set_changed", "tools_changed", "system_changed", "tool_choice_changed",
    "thinking_or_output_config_changed", "message_prefix_changed", "compaction_or_relief_rewrite",
    "anchor_identity_changed", "expected_after_eviction_intent", "ttl_5m_may_have_expired",
    "ttl_1h_may_have_expired", "unchanged_short_gap_likely_server_side", "partial_hit_prefix_changed",
    "cache_read_observed", "cache_write_without_read_observed", "no_cache_usage_reported",
    "compatible_provider_unknown",
}
_PROVIDER_REASONS = {
    "not_requested", "warmup_no_comparison", "no_divergence", "comparison_pending", "model_changed",
    "system_changed", "tools_changed", "messages_changed", "previous_message_not_found", "unavailable", "unknown",
}
_STATE_STATUSES = {"committed", "stale", "unavailable", "recorded"}


def promptCacheEventPayload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": _closed(data.get("phase"), _PHASES) or "state_error",
        "mode": _closed(data.get("mode"), _MODES),
        "ttl": _closed(data.get("ttl"), _TTLS),
        "scope": _closed(data.get("scope"), _SCOPES),
        "usedBreakpoints": _count(data.get("used_breakpoints"), 4),
        "placements": _placements(data.get("placements")),
        "cacheReadInputTokens": _count(data.get("cache_read_input_tokens")),
        "cacheCreationInputTokens": _count(data.get("cache_creation_input_tokens")),
        "cacheCreation5mInputTokens": _count(data.get("cache_creation_5m_input_tokens")),
        "cacheCreation1hInputTokens": _count(data.get("cache_creation_1h_input_tokens")),
        "cacheCreationTtlDetailComplete": data.get("cache_creation_ttl_detail_complete") if isinstance(data.get("cache_creation_ttl_detail_complete"), bool) else None,
        "localReason": _closed(data.get("local_reason"), _LOCAL_REASONS),
        "providerReason": _closed(data.get("provider_reason"), _PROVIDER_REASONS),
        "stateStatus": _closed(data.get("state_status"), _STATE_STATUSES),
        "intentStatus": "recorded" if data.get("intent_status") == "recorded" else None,
    }


def _closed(value: Any, allowed: set[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _count(value: Any, maximum: int = (1 << 53) - 1) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return min(int(value), maximum)


def _placements(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if item in _PLACEMENTS and item not in result:
            result.append(item)
        if len(result) == 4:
            break
    return result



