from __future__ import annotations

import math
import re
from typing import Any, Callable


DEFAULT_PROVIDER_MAX_CONTEXT_LENGTH = 200_000
DEFAULT_PROVIDER_MAX_OUTPUT_TOKENS = 16_384
DEFAULT_COMPACTION_THRESHOLD_TOKENS = 200_000
DEFAULT_COMPACTION_PRESERVATION_RATIO = 0.1
DEFAULT_OUTPUT_BUDGET_SAFETY_MARGIN_TOKENS = 4_096
MIN_USEFUL_PROVIDER_OUTPUT_TOKENS = 256


def normalize_positive_integer(value: Any) -> int | None:
    try:
        parsed = float(value.strip()) if isinstance(value, str) and value.strip() else float(value)
    except Exception:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return math.floor(parsed)


def resolve_provider_token_limits(input: dict[str, Any] | None = None) -> dict[str, int]:
    profile = input or {}
    explicit_context = normalize_positive_integer(profile.get("maxContextLength"))
    explicit_output = normalize_positive_integer(profile.get("maxOutputTokens"))
    inferred = _infer_model_token_limits(profile) or {}
    return {
        "maxContextLength": explicit_context or inferred.get("maxContextLength") or DEFAULT_PROVIDER_MAX_CONTEXT_LENGTH,
        "maxOutputTokens": explicit_output or inferred.get("maxOutputTokens") or DEFAULT_PROVIDER_MAX_OUTPUT_TOKENS,
    }


def resolve_compaction_policy(input: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = input or {}
    limits = resolve_provider_token_limits(profile)
    explicit_threshold = normalize_positive_integer(profile.get("compactionThresholdTokens"))
    explicit_output = normalize_positive_integer(profile.get("compactionOutputTokens"))
    raw_threshold = explicit_threshold or DEFAULT_COMPACTION_THRESHOLD_TOKENS
    threshold = min(raw_threshold, limits["maxContextLength"])
    default_output = math.ceil(threshold * DEFAULT_COMPACTION_PRESERVATION_RATIO)
    raw_output = explicit_output or min(limits["maxOutputTokens"], default_output)
    return {
        "maxContextLength": limits["maxContextLength"],
        "maxOutputTokens": limits["maxOutputTokens"],
        "compactionThresholdTokens": threshold,
        "compactionOutputTokens": min(raw_output, limits["maxOutputTokens"]),
        "thresholdSource": "default" if explicit_threshold is None else "explicit",
        "outputSource": "default" if explicit_output is None else "explicit",
        "thresholdClamped": raw_threshold > limits["maxContextLength"],
        "outputClamped": raw_output > limits["maxOutputTokens"],
    }


def resolve_compaction_threshold_tokens(input: dict[str, Any] | None = None) -> int:
    return int(resolve_compaction_policy(input).get("compactionThresholdTokens") or DEFAULT_COMPACTION_THRESHOLD_TOKENS)


def resolve_compaction_output_tokens(input: dict[str, Any] | None = None) -> int:
    return int(resolve_compaction_policy(input).get("compactionOutputTokens") or DEFAULT_PROVIDER_MAX_OUTPUT_TOKENS)


def resolve_compaction_request_output_tokens(args: dict[str, Any] | None = None, **kwargs: Any) -> int:
    args = {**(args or {}), **kwargs}
    policy = resolve_compaction_policy(args.get("profile") or {})
    source_estimate = normalize_positive_integer(args.get("sourceTokenEstimate")) or 0
    preservation_target = math.ceil(source_estimate * DEFAULT_COMPACTION_PRESERVATION_RATIO)
    return min(policy["maxOutputTokens"], max(policy["compactionOutputTokens"], preservation_target))


def resolve_provider_profile_token_limits(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, int]:
    args = {**(args or {}), **kwargs}
    profile = args.get("profile") or {}
    return resolve_provider_token_limits(
        {
            "profileName": args.get("profileName"),
            "kind": profile.get("kind"),
            "apiMode": args.get("apiMode") or profile.get("apiMode"),
            "model": args.get("model") or profile.get("defaultModel"),
            "maxContextLength": profile.get("maxContextLength"),
            "maxOutputTokens": profile.get("maxOutputTokens"),
            "compactionThresholdTokens": profile.get("compactionThresholdTokens"),
            "compactionOutputTokens": profile.get("compactionOutputTokens"),
        }
    )


def resolve_context_token_limit(profile: dict[str, Any] | None = None) -> int:
    if not profile:
        return DEFAULT_PROVIDER_MAX_CONTEXT_LENGTH
    return normalize_positive_integer(profile.get("maxContextLength")) or DEFAULT_PROVIDER_MAX_CONTEXT_LENGTH


def compute_provider_token_budget(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    limits = resolve_provider_token_limits(args.get("profile") or {})
    estimated_input_tokens = max(0, math.floor(float(args.get("estimatedInputTokens") or 0)))
    safety_margin = max(
        0,
        math.floor(float(args.get("safetyMarginTokens") if args.get("safetyMarginTokens") is not None else DEFAULT_OUTPUT_BUDGET_SAFETY_MARGIN_TOKENS)),
    )
    min_useful = max(
        1,
        math.floor(float(args.get("minUsefulOutputTokens") if args.get("minUsefulOutputTokens") is not None else MIN_USEFUL_PROVIDER_OUTPUT_TOKENS)),
    )
    remaining = limits["maxContextLength"] - estimated_input_tokens - safety_margin
    effective = max(0, min(limits["maxOutputTokens"], remaining))
    return {
        "maxContextLength": limits["maxContextLength"],
        "configuredMaxOutputTokens": limits["maxOutputTokens"],
        "effectiveMaxOutputTokens": effective,
        "estimatedInputTokens": estimated_input_tokens,
        "safetyMarginTokens": safety_margin,
        "remainingOutputTokens": remaining,
        "clamped": effective < limits["maxOutputTokens"],
        "insufficient": effective < min_useful,
        "minUsefulOutputTokens": min_useful,
    }


def _infer_model_token_limits(input: dict[str, Any]) -> dict[str, int] | None:
    normalized = {
        "profileName": str(input.get("profileName") or "").strip().lower(),
        "kind": input.get("kind"),
        "apiMode": input.get("apiMode"),
        "model": str(input.get("model") or "").strip().lower(),
    }
    for max_context, max_output, predicate in _MODEL_LIMIT_RULES:
        if predicate(normalized):
            return {"maxContextLength": max_context, "maxOutputTokens": max_output}
    return None


def _rule(max_context: int, max_output: int, predicate: Callable[[dict[str, Any]], bool]) -> tuple[int, int, Callable[[dict[str, Any]], bool]]:
    return (max_context, max_output, predicate)


_MODEL_LIMIT_RULES = [
    _rule(400_000, 128_000, lambda item: item["profileName"] == "codex" or item["apiMode"] == "codex" or (re.match(r"^gpt-5\.", item["model"]) and "codex" in item["model"])),
    _rule(1_000_000, 128_000, lambda item: item["kind"] == "openai" and item["apiMode"] != "codex" and re.match(r"^gpt-5(?:\.|-|$)", item["model"])),
    _rule(1_000_000, 128_000, lambda item: item["kind"] == "anthropic" and re.match(r"^claude-(?:opus|sonnet|haiku)-4(?:-|$)", item["model"])),
    _rule(1_000_000, 131_072, lambda item: "zai-glm-5-2" in item["profileName"] or re.search(r"\bglm-5\.?2\b", item["model"])),
    _rule(202_752, 131_072, lambda item: "glm-5-1" in item["profileName"] or re.search(r"(?:glm-5\.1|glm-5p1)", item["model"])),
    _rule(1_048_576, 131_072, lambda item: "mimo-2-5-pro" in item["profileName"] or re.search(r"mimo-v?2\.5-pro", item["model"])),
    _rule(262_144, 65_536, lambda item: "nemotron-3-ultra" in item["profileName"] or "nemotron-3-ultra" in item["model"]),
    _rule(512_000, 512_000, lambda item: "minimax-3" in item["profileName"] or "minimax-m3" in item["model"]),
    _rule(262_144, 262_144, lambda item: "kimi-2-5" in item["profileName"] or "kimi-k2p5" in item["model"]),
    _rule(1_000_000, 1_000_000, lambda item: "grok-4-3" in item["profileName"] or "grok-4.3" in item["model"]),
    _rule(256_000, 256_000, lambda item: "grok-build-0-1" in item["profileName"] or "grok-build-0.1" in item["model"]),
    _rule(262_144, 262_144, lambda item: "kimi-2-7-coding" in item["profileName"] or "kimi-k2.7-coding" in item["model"]),
    _rule(1_000_000, 384_000, lambda item: "deepseek-v4" in item["profileName"] or "deepseek-v4" in item["model"]),
    _rule(131_072, 32_768, lambda item: "llamacpp" in item["profileName"] or item["model"] == "llamacpp"),
    _rule(262_144, 65_536, lambda item: "qwen-3-6-plus" in item["profileName"] or "qwen3p6-plus" in item["model"]),
]


normalizePositiveInteger = normalize_positive_integer
resolveProviderTokenLimits = resolve_provider_token_limits
resolveCompactionPolicy = resolve_compaction_policy
resolveCompactionThresholdTokens = resolve_compaction_threshold_tokens
resolveCompactionOutputTokens = resolve_compaction_output_tokens
resolveCompactionRequestOutputTokens = resolve_compaction_request_output_tokens
resolveProviderProfileTokenLimits = resolve_provider_profile_token_limits
resolveContextTokenLimit = resolve_context_token_limit
computeProviderTokenBudget = compute_provider_token_budget
