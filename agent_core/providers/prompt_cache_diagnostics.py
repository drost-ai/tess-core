from __future__ import annotations

from ._core import finite_nonnegative_int as _finite_nonnegative

import hashlib
import json
import math
import copy
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


MAX_MESSAGE_HASHES = 64
MAX_BLOCK_HASHES = 128
MAX_TOOL_HASHES = 128
MAX_BETA_HASHES = 32
MAX_REASON_DELTAS = 16
MAX_RESPONSE_ID_BYTES = 256
MAX_PROMPT_CACHE_STATE_BYTES = 128 * 1024

# The single source of the provider-diagnosis vocabulary; validators import
# these instead of transcribing the sets.
PROMPT_CACHE_MISS_REASONS = frozenset(
    {
        "model_changed",
        "system_changed",
        "tools_changed",
        "messages_changed",
        "previous_message_not_found",
        "unavailable",
    }
)
PROMPT_CACHE_PROVIDER_REASONS = PROMPT_CACHE_MISS_REASONS | {
    "not_requested",
    "warmup_no_comparison",
    "no_divergence",
    "comparison_pending",
    "unknown",
}


def buildPromptCacheRequestSnapshot(input: dict[str, Any]) -> dict[str, Any]:
    messages = input.get("normalizedMessages") or []
    tools = input.get("normalizedTools") or []
    system = input.get("normalizedSystem") or []
    betas = [str(token) for token in input.get("betaTokens") or []]
    message_hashes = [
        hashPromptCacheValue("message", _semantic_message(message)) for message in messages
    ]
    block_hashes = [
        hashPromptCacheValue("block", _strip_decoration(block))
        for message in messages
        for block in message.get("content") or []
    ]
    tool_hashes = [hashPromptCacheValue("tool", _strip_decoration(tool)) for tool in tools]
    beta_hashes = [hashPromptCacheValue("beta", token) for token in betas]
    omitted = message_hashes[: max(0, len(message_hashes) - MAX_MESSAGE_HASHES)]
    semantic_prefix = {
        "tools": [_strip_decoration(tool) for tool in tools],
        "system": [_strip_decoration(block) for block in system],
        "messages": [_semantic_message(message) for message in messages],
        "toolChoice": input.get("toolChoice"),
        "outputConfig": input.get("outputConfig"),
    }
    plan = input.get("plan") or {}
    policy = input.get("policy") or {}
    snapshot = {
        "schemaVersion": 1,
        "providerProfileNameHash": hashPromptCacheValue(
            "profile", str(input.get("providerProfileName") or "")
        ),
        "routeClass": input.get("routeClass") or "official",
        "endpointIdentityHash": hashPromptCacheValue(
            "endpoint", _normalize_endpoint_identity(input.get("baseUrl"))
        ),
        "modelHash": hashPromptCacheValue("model", str(input.get("model") or "")),
        "betasHash": hashPromptCacheValue("betas", betas),
        "toolsHash": hashPromptCacheValue("tools", semantic_prefix["tools"]),
        "systemHash": hashPromptCacheValue("system", semantic_prefix["system"]),
        "messagesHash": hashPromptCacheValue("messages", semantic_prefix["messages"]),
        "toolChoiceHash": hashPromptCacheValue("tool-choice", input.get("toolChoice")),
        "outputConfigHash": hashPromptCacheValue("output-config", input.get("outputConfig")),
        "cachePolicyHash": hashPromptCacheValue(
            "policy", _semantic_policy(policy, plan)
        ),
        "semanticPrefixHash": hashPromptCacheValue("semantic-prefix", semantic_prefix),
        "messageCount": len(messages),
        "blockCount": len(block_hashes),
        "toolCount": len(tools),
        "betaCount": len(betas),
        "boundedMessageHashes": message_hashes[-MAX_MESSAGE_HASHES:],
        "boundedBlockHashes": block_hashes[-MAX_BLOCK_HASHES:],
        "boundedToolHashes": tool_hashes[-MAX_TOOL_HASHES:],
        "boundedBetaHashes": beta_hashes[-MAX_BETA_HASHES:],
        "preparation": input.get("preparation") or "normal",
        "reactiveAttempt": max(0, int(input.get("reactiveAttempt") or 0)),
    }
    if omitted:
        snapshot["omittedPrefixHash"] = hashPromptCacheValue(
            "omitted-message-prefix", omitted
        )
    if isinstance(plan.get("tailA"), dict):
        snapshot["tailA"] = copy.deepcopy(plan["tailA"])
    if isinstance(plan.get("tailB"), dict):
        snapshot["tailB"] = copy.deepcopy(plan["tailB"])
    return snapshot


def diagnosePromptCache(args: dict[str, Any]) -> dict[str, Any]:
    previous = args.get("previous")
    current = args["current"]
    if not isinstance(previous, dict):
        return _diagnosis("warmup_no_previous_state", "deterministic")
    if previous.get("schemaVersion") != current.get("schemaVersion"):
        return _diagnosis("state_schema_changed", "deterministic", ["schema"])
    checks = (
        ("modelHash", "model_changed", "model"),
        ("cachePolicyHash", "cache_policy_changed", "cache_policy"),
        ("betasHash", "beta_set_changed", "betas"),
        ("toolsHash", "tools_changed", "tools"),
        ("systemHash", "system_changed", "system"),
        ("toolChoiceHash", "tool_choice_changed", "tool_choice"),
        (
            "outputConfigHash",
            "thinking_or_output_config_changed",
            "output_config",
        ),
    )
    if any(
        previous.get(field) != current.get(field)
        for field in ("providerProfileNameHash", "routeClass", "endpointIdentityHash")
    ):
        return _diagnosis("provider_route_changed", "deterministic", ["route"])
    for field, reason, delta in checks:
        if previous.get(field) != current.get(field):
            return _diagnosis(reason, "deterministic", [delta])
    prefix_continues = bool(
        isinstance(previous.get("tailA"), dict)
        and isinstance(current.get("tailB"), dict)
        and previous["tailA"].get("identity") == current["tailB"].get("identity")
    )
    messages_changed = previous.get("messagesHash") != current.get("messagesHash")
    if messages_changed and not prefix_continues:
        if current.get("preparation") != "normal" or previous.get("preparation") != "normal":
            return _diagnosis("compaction_or_relief_rewrite", "deterministic", ["messages"])
        if previous.get("tailA"):
            return _diagnosis("anchor_identity_changed", "deterministic", ["tail_anchor"])
        return _diagnosis("message_prefix_changed", "deterministic", ["messages"])
    usage = args.get("usage") if isinstance(args.get("usage"), dict) else {}
    read = _finite_nonnegative(usage.get("cache_read_input_tokens"))
    creation = _finite_nonnegative(usage.get("cache_creation_input_tokens"))
    if read > 0 and creation > 0 and messages_changed:
        return _diagnosis("partial_hit_prefix_changed", "likely", ["messages"])
    if read > 0:
        return _diagnosis("cache_read_observed", "deterministic")
    if creation > 0:
        return _diagnosis("cache_write_without_read_observed", "deterministic")
    if args.get("evictionPending"):
        return _diagnosis("expected_after_eviction_intent", "likely")
    if args.get("compatibility") == "compatible":
        return _diagnosis("compatible_provider_unknown", "inconclusive")
    elapsed_ms = _elapsed_ms(args.get("previousCommittedAt"), args.get("now"))
    ttl = args.get("ttl")
    if ttl in {"5m", "provider_default"} and elapsed_ms >= 300_000:
        return _diagnosis("ttl_5m_may_have_expired", "likely")
    if ttl == "1h" and elapsed_ms >= 3_600_000:
        return _diagnosis("ttl_1h_may_have_expired", "likely")
    if isinstance(args.get("usage"), dict):
        return _diagnosis("unchanged_short_gap_likely_server_side", "inconclusive")
    return _diagnosis("no_cache_usage_reported", "inconclusive")


def normalizePromptCacheProviderReason(args: dict[str, Any]) -> str:
    if not args.get("requested"):
        return "not_requested"
    if not args.get("fieldPresent"):
        return "unavailable"
    diagnostics = args.get("diagnostics")
    if diagnostics is None:
        return "no_divergence" if args.get("previousMessageIdSent") else "warmup_no_comparison"
    if not isinstance(diagnostics, dict) or "cache_miss_reason" not in diagnostics:
        return "unavailable"
    reason = diagnostics.get("cache_miss_reason")
    if reason is None:
        return "comparison_pending"
    kind = reason.get("type") if isinstance(reason, dict) else None
    return kind if kind in PROMPT_CACHE_MISS_REASONS else "unknown"


def canonicalPromptCacheJson(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def hashPromptCacheValue(domain: str, value: Any) -> str:
    payload = f"agent-core:prompt-cache:{domain}:v1\0{canonicalPromptCacheJson(value)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    return None


def _strip_decoration(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = copy.deepcopy(value)
    result.pop("cache_control", None)
    result.pop("diagnostics", None)
    result.pop("_cache_target", None)
    return result


def _semantic_message(message: Any) -> dict[str, Any]:
    record = message if isinstance(message, dict) else {}
    return {
        "role": record.get("role"),
        "content": [_strip_decoration(block) for block in record.get("content") or []],
    }


def _semantic_policy(policy: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(policy.get("enabled")),
        "mode": plan.get("mode"),
        "ttl": policy.get("ttl") or "provider_default",
        "scope": policy.get("scope") or "ephemeral",
        "compatibility": policy.get("compatibility") or "auto",
        "explicit": policy.get("explicit"),
        "messageTailBreakpoints": policy.get("messageTailBreakpoints"),
        "toolDefinitionBreakpoint": policy.get("toolDefinitionBreakpoint"),
        "providerDiagnostics": bool(policy.get("providerDiagnostics")),
        "automatic": bool(plan.get("automatic")),
        "system": bool(plan.get("system")),
        "tools": bool(plan.get("tools")),
    }


def _normalize_endpoint_identity(value: Any) -> str:
    raw = str(value or "https://api.anthropic.com/v1")
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return "invalid-compatible-endpoint"
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def _diagnosis(reason: str, confidence: str, deltas: list[str] | None = None) -> dict[str, Any]:
    return {"reason": reason, "confidence": confidence, "deltas": (deltas or [])[:MAX_REASON_DELTAS]}




def _elapsed_ms(value: Any, now: Any) -> int:
    try:
        before = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    current = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0, int((current - before).total_seconds() * 1000))


