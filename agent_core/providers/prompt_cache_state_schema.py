from __future__ import annotations

import datetime as _datetime
import math
import re
from typing import Any


_HASH = re.compile(r"^[a-f0-9]{64}$")


def isPromptCacheStateV1(value: Any) -> bool:
    state = _record(value)
    if not _only(state, {"schemaVersion", "sessionId", "revision", "committedAt", "dispatchOrder", "request", "placement", "response", "diagnosis", "eviction"}):
        return False
    return bool(
        state.get("schemaVersion") == 1
        and _bounded(state.get("sessionId"), 1024)
        and _positive_int(state.get("revision"))
        and _timestamp(state.get("committedAt"))
        and _dispatch(state.get("dispatchOrder"))
        and _request(state.get("request"))
        and _placement(state.get("placement"))
        and _response(state.get("response"))
        and _diagnosis(state.get("diagnosis"))
        and ("eviction" not in state or _eviction(state.get("eviction")))
    )


def _dispatch(value: Any) -> bool:
    order = _record(value)
    return bool(
        _only(order, {"runtimeGeneration", "messageCount", "reactiveAttempt", "startedAtMs", "localSequence", "nonce"})
        and _positive_int(order.get("runtimeGeneration"))
        and all(_nonnegative_int(order.get(key)) for key in ("messageCount", "reactiveAttempt", "startedAtMs", "localSequence"))
        and _bounded(order.get("nonce"), 128)
    )


def _request(value: Any) -> bool:
    request = _record(value)
    allowed = {
        "schemaVersion", "providerProfileNameHash", "routeClass", "endpointIdentityHash", "modelHash",
        "betasHash", "toolsHash", "systemHash", "messagesHash", "toolChoiceHash", "outputConfigHash",
        "cachePolicyHash", "semanticPrefixHash", "messageCount", "blockCount", "toolCount", "betaCount",
        "boundedMessageHashes", "boundedBlockHashes", "boundedToolHashes", "boundedBetaHashes",
        "omittedPrefixHash", "preparation", "reactiveAttempt", "tailA", "tailB",
    }
    hashes = (
        "providerProfileNameHash", "endpointIdentityHash", "modelHash", "betasHash", "toolsHash", "systemHash",
        "messagesHash", "toolChoiceHash", "outputConfigHash", "cachePolicyHash", "semanticPrefixHash",
    )
    if not _only(request, allowed) or request.get("schemaVersion") != 1:
        return False
    if request.get("routeClass") not in {"official", "claude_code", "compatible", "bedrock"}:
        return False
    if request.get("preparation") not in {"normal", "compacted", "reactive_relief", "repaired"}:
        return False
    if not all(_valid_hash(request.get(key)) for key in hashes):
        return False
    if not all(_nonnegative_int(request.get(key)) for key in ("messageCount", "blockCount", "toolCount", "betaCount", "reactiveAttempt")):
        return False
    for key, limit in (("boundedMessageHashes", 64), ("boundedBlockHashes", 128), ("boundedToolHashes", 128), ("boundedBetaHashes", 32)):
        if not _hashes(request.get(key), limit):
            return False
    if "omittedPrefixHash" in request and not _valid_hash(request.get("omittedPrefixHash")):
        return False
    return all(key not in request or _anchor(request.get(key)) for key in ("tailA", "tailB"))


def _placement(value: Any) -> bool:
    placement = _record(value)
    placements = placement.get("placements")
    return bool(
        _only(placement, {"mode", "ttl", "scope", "placements"})
        and placement.get("mode") in {"off", "explicit", "auto"}
        and placement.get("ttl") in {"provider_default", "5m", "1h", "24h"}
        and placement.get("scope") in {"ephemeral", "global", "bedrock"}
        and isinstance(placements, list)
        and len(placements) <= 4
        and all(item in {"automatic", "system", "tools", "tail_a", "tail_b"} for item in placements)
    )


def _response(value: Any) -> bool:
    response = _record(value)
    allowed_reasons = {
        "not_requested", "warmup_no_comparison", "no_divergence", "comparison_pending", "model_changed",
        "system_changed", "tools_changed", "messages_changed", "previous_message_not_found", "unavailable", "unknown",
    }
    return bool(
        _only(response, {"id", "cacheReadInputTokens", "cacheCreationInputTokens", "cacheCreation5mInputTokens", "cacheCreation1hInputTokens", "providerDiagnosis"})
        and all(_nonnegative_number(response.get(key)) for key in ("cacheReadInputTokens", "cacheCreationInputTokens", "cacheCreation5mInputTokens", "cacheCreation1hInputTokens"))
        and ("id" not in response or _bounded(response.get("id"), 256))
        and ("providerDiagnosis" not in response or response.get("providerDiagnosis") in allowed_reasons)
    )


def _diagnosis(value: Any) -> bool:
    diagnosis = _record(value)
    reasons = {
        "warmup_no_previous_state", "state_schema_changed", "provider_route_changed", "model_changed", "cache_policy_changed",
        "beta_set_changed", "tools_changed", "system_changed", "tool_choice_changed", "thinking_or_output_config_changed",
        "message_prefix_changed", "compaction_or_relief_rewrite", "anchor_identity_changed", "expected_after_eviction_intent",
        "ttl_5m_may_have_expired", "ttl_1h_may_have_expired", "unchanged_short_gap_likely_server_side",
        "partial_hit_prefix_changed", "cache_read_observed", "cache_write_without_read_observed", "no_cache_usage_reported",
        "compatible_provider_unknown",
    }
    return bool(
        _only(diagnosis, {"localReason", "confidence"})
        and diagnosis.get("localReason") in reasons
        and diagnosis.get("confidence") in {"deterministic", "likely", "inconclusive"}
    )


def _eviction(value: Any) -> bool:
    eviction = _record(value)
    return bool(
        _only(eviction, {"pending", "requestedAt", "reason", "finalizationKey"})
        and isinstance(eviction.get("pending"), bool)
        and _timestamp(eviction.get("requestedAt"))
        and eviction.get("reason") == "terminal_session_completion"
        and _bounded(eviction.get("finalizationKey"), 256)
    )


def _anchor(value: Any) -> bool:
    anchor = _record(value)
    return bool(
        _only(anchor, {"messageIndex", "blockIndex", "identity"})
        and _nonnegative_int(anchor.get("messageIndex"))
        and _nonnegative_int(anchor.get("blockIndex"))
        and _valid_hash(anchor.get("identity"))
    )


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _only(value: dict[str, Any], keys: set[str]) -> bool:
    return bool(value) and set(value).issubset(keys)


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def _hashes(value: Any, maximum: int) -> bool:
    return isinstance(value, list) and len(value) <= maximum and all(_valid_hash(item) for item in value)


def _bounded(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= maximum


def _nonnegative_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def _nonnegative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _positive_int(value: Any) -> bool:
    return _nonnegative_int(value) and value > 0


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


