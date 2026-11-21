from __future__ import annotations

from ._knob_reader import provider_knob

import math
import re
from typing import Any, Callable


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
        "maxContextLength": explicit_context or inferred.get("maxContextLength") or int(provider_knob("provider.max_context_length")),
        "maxOutputTokens": explicit_output or inferred.get("maxOutputTokens") or _fallback_max_output_tokens(),
    }


def resolve_request_max_output_tokens(request: dict[str, Any]) -> int:
    """Max output tokens a provider request should carry on the wire."""
    for key in ("maxOutputTokens", "max_output_tokens", "maxTokens", "max_tokens"):
        value = request.get(key)
        try:
            parsed = int(float(str(value).strip())) if str(value).strip() else 0
        except Exception:
            continue
        if parsed > 0:
            return parsed
    return _fallback_max_output_tokens()


def _fallback_max_output_tokens() -> int:
    # Call-time import: the config package imports this module at load.
    from ..config._knobs import knob_value

    return int(knob_value("provider.max_output_tokens"))


def resolve_compaction_policy(input: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = input or {}
    limits = resolve_provider_token_limits(profile)
    explicit_threshold = normalize_positive_integer(profile.get("compactionThresholdTokens"))
    explicit_output = normalize_positive_integer(profile.get("compactionOutputTokens"))
    target_present = (
        "compactionTargetTokens" in profile
        and profile.get("compactionTargetTokens") is not None
    )
    explicit_target = normalize_positive_integer(profile.get("compactionTargetTokens"))
    if target_present and explicit_target is None:
        raise ValueError("compactionTargetTokens must be a positive integer")
    raw_threshold = explicit_threshold or int(provider_knob("provider.compaction_threshold_tokens"))
    threshold = min(raw_threshold, limits["maxContextLength"])
    default_output = math.ceil(threshold * float(provider_knob("provider.compaction_preservation_ratio")))
    raw_output = explicit_output or min(limits["maxOutputTokens"], default_output)
    output = min(raw_output, limits["maxOutputTokens"])
    derived_headroom = max(
        output,
        math.ceil(threshold * float(provider_knob("provider.compaction_headroom_ratio"))),
    )
    target = explicit_target or (threshold - derived_headroom)
    if target <= 0 or target >= threshold:
        raise ValueError(
            "compactionTargetTokens must be greater than zero and less than "
            "compactionThresholdTokens"
        )
    return {
        "maxContextLength": limits["maxContextLength"],
        "maxOutputTokens": limits["maxOutputTokens"],
        "compactionThresholdTokens": threshold,
        "compactionTargetTokens": target,
        "compactionOutputTokens": output,
        "compactionHeadroomTokens": threshold - target,
        "thresholdSource": "default" if explicit_threshold is None else "explicit",
        "targetSource": "explicit" if explicit_target is not None else "derived",
        "outputSource": "default" if explicit_output is None else "explicit",
        "thresholdClamped": raw_threshold > limits["maxContextLength"],
        "outputClamped": raw_output > limits["maxOutputTokens"],
    }


def resolve_interactive_context_policy(input: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = input or {}
    relief_configured = any(
        key in profile and profile.get(key) is not None
        for key in ("contextReliefThresholdTokens", "contextReliefTargetTokens")
    )
    turn_budget_configured = (
        "inlineToolResultTurnBytes" in profile
        and profile.get("inlineToolResultTurnBytes") is not None
    )

    return {
        # Admission-time interactive relief is retired; the policy resolver stays for config compatibility.
        # Historical keys remain accepted as inert compatibility input.
        "contextReliefThresholdTokens": None,
        "contextReliefTargetTokens": None,
        # Tool results are no longer shared out of a batch or turn allowance: each is held to its
        # own tool's limit. Both keys stay accepted as inert compatibility input.
        "inlineToolResultBatchBytes": None,
        "inlineToolResultTurnBytes": None,
        "reliefSource": "deprecated_ignored"
        if relief_configured
        else "disabled",
        "toolBudgetSource": (
            "deprecated_ignored" if profile.get("inlineToolResultBatchBytes") is not None else "disabled"
        ),
        "deprecatedTurnBudgetIgnored": turn_budget_configured,
    }


def resolve_compaction_threshold_tokens(input: dict[str, Any] | None = None) -> int:
    return int(resolve_compaction_policy(input).get("compactionThresholdTokens") or int(provider_knob("provider.compaction_threshold_tokens")))


def resolve_compaction_output_tokens(input: dict[str, Any] | None = None) -> int:
    return int(resolve_compaction_policy(input).get("compactionOutputTokens") or _fallback_max_output_tokens())


def resolve_compaction_request_output_tokens(args: dict[str, Any] | None = None) -> int:
    args = dict(args or {})
    policy = resolve_compaction_policy(args.get("profile") or {})
    source_estimate = normalize_positive_integer(args.get("sourceTokenEstimate")) or 0
    preservation_target = math.ceil(source_estimate * float(provider_knob("provider.compaction_preservation_ratio")))
    return min(policy["maxOutputTokens"], max(policy["compactionOutputTokens"], preservation_target))


def resolve_provider_profile_token_limits(args: dict[str, Any] | None = None) -> dict[str, int]:
    args = dict(args or {})
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
            "compactionTargetTokens": profile.get("compactionTargetTokens"),
            "compactionOutputTokens": profile.get("compactionOutputTokens"),
        }
    )


def resolve_context_token_limit(profile: dict[str, Any] | None = None) -> int:
    if not profile:
        return int(provider_knob("provider.max_context_length"))
    return normalize_positive_integer(profile.get("maxContextLength")) or int(provider_knob("provider.max_context_length"))


def compute_provider_token_budget(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    limits = resolve_provider_token_limits(args.get("profile") or {})
    estimated_input_tokens = max(0, math.floor(float(args.get("estimatedInputTokens") or 0)))
    safety_margin = max(
        0,
        math.floor(float(args.get("safetyMarginTokens") if args.get("safetyMarginTokens") is not None else int(provider_knob("provider.output_budget_safety_margin_tokens")))),
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
    for rule in _configured_model_limit_rules:
        if rule["match"] in normalized["model"] or rule["match"] in normalized["profileName"]:
            return {key: value for key, value in rule.items() if key != "match"}
    for max_context, max_output, predicate in _MODEL_LIMIT_RULES:
        if predicate(normalized):
            return {"maxContextLength": max_context, "maxOutputTokens": max_output}
    return None



def _explicit_positive_integer(input: dict[str, Any], key: str) -> int | None:
    if key not in input or input.get(key) is None:
        return None
    value = normalize_positive_integer(input.get(key))
    if value is None:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _rule(max_context: int, max_output: int, predicate: Callable[[dict[str, Any]], bool]) -> tuple[int, int, Callable[[dict[str, Any]], bool]]:
    return (max_context, max_output, predicate)


_configured_model_limit_rules: list[dict[str, Any]] = []


def configure_model_limit_rules(rules: Any) -> None:
    """Install providers.yaml ``modelLimitRules`` process-wide.

    Each rule: {match: <substring>, maxContextLength and/or maxOutputTokens}.
    The match is case-insensitive against the model name and the profile
    name; user rules are consulted before the builtin table, and explicit
    profile fields still beat every rule. Malformed rules fail loud at
    settings load.
    """

    validated: list[dict[str, Any]] = []
    for index, rule in enumerate(rules or []):
        if not isinstance(rule, dict) or not isinstance(rule.get("match"), str) or not rule["match"].strip():
            raise ValueError(f"modelLimitRules[{index}]: each rule needs a non-empty 'match' string.")
        unknown = set(rule) - {"match", "maxContextLength", "maxOutputTokens"}
        if unknown:
            raise ValueError(f"modelLimitRules[{index}]: unknown key(s): {', '.join(sorted(unknown))}.")
        entry: dict[str, Any] = {"match": rule["match"].strip().lower()}
        for key in ("maxContextLength", "maxOutputTokens"):
            if rule.get(key) is not None:
                value = normalize_positive_integer(rule.get(key))
                if value is None:
                    raise ValueError(f"modelLimitRules[{index}].{key} must be a positive integer.")
                entry[key] = value
        if len(entry) == 1:
            raise ValueError(f"modelLimitRules[{index}]: declare maxContextLength and/or maxOutputTokens.")
        validated.append(entry)
    _configured_model_limit_rules[:] = validated


def reset_configured_model_limit_rules() -> None:
    _configured_model_limit_rules.clear()


_MODEL_LIMIT_RULES = [
    _rule(400_000, 128_000, lambda item: item["profileName"] == "codex" or item["apiMode"] == "codex" or (re.match(r"^gpt-5\.", item["model"]) and "codex" in item["model"])),
    _rule(1_000_000, 128_000, lambda item: item["kind"] == "openai" and item["apiMode"] != "codex" and re.match(r"^gpt-5(?:\.|-|$)", item["model"])),
    _rule(1_000_000, 128_000, lambda item: item["kind"] == "anthropic" and re.match(r"^claude-(?:opus|sonnet|haiku)-4(?:-|$)", item["model"])),
    # Gemini: a 1,048,576-token window (shared by input, thinking and output on Gemini 4)
    # and 65,536 output tokens in a synchronous response.
    _rule(1_048_576, 65_536, lambda item: item["kind"] == "google" and item["model"].startswith("gemini-")),
    _rule(1_000_000, 131_072, lambda item: "zai-glm-5-2" in item["profileName"] or re.search(r"\bglm-5\.?2\b", item["model"])),
    _rule(202_752, 131_072, lambda item: "glm-5-1" in item["profileName"] or re.search(r"(?:glm-5\.1|glm-5p1)", item["model"])),
    _rule(1_048_576, 131_072, lambda item: "mimo-2-5-pro" in item["profileName"] or re.search(r"mimo-v?2\.5-pro", item["model"])),
    _rule(262_144, 65_536, lambda item: "nemotron-3-ultra" in item["profileName"] or "nemotron-3-ultra" in item["model"]),
    _rule(512_000, 512_000, lambda item: "minimax-3" in item["profileName"] or "minimax-m3" in item["model"] or "minimax-m2p7" in item["model"]),
    _rule(262_144, 262_144, lambda item: "kimi-2-5" in item["profileName"] or "kimi-k2p5" in item["model"]),
    _rule(262_144, 262_144, lambda item: "kimi-2-6" in item["profileName"] or "kimi-k2.6" in item["model"]),
    _rule(1_000_000, 1_000_000, lambda item: "grok-4-3" in item["profileName"] or "grok-4.3" in item["model"]),
    _rule(256_000, 256_000, lambda item: "grok-build-0-1" in item["profileName"] or "grok-build-0.1" in item["model"]),
    _rule(262_144, 262_144, lambda item: "kimi-2-7-coding" in item["profileName"] or "kimi-k2.7-coding" in item["model"]),
    _rule(1_000_000, 384_000, lambda item: "deepseek-v4" in item["profileName"] or "deepseek-v4" in item["model"]),
    _rule(131_072, 32_768, lambda item: "llamacpp" in item["profileName"] or item["model"] == "llamacpp"),
    _rule(262_144, 65_536, lambda item: "qwen-3-6-plus" in item["profileName"] or "qwen3p6-plus" in item["model"]),
]


normalizePositiveInteger = normalize_positive_integer
resolveProviderTokenLimits = resolve_provider_token_limits
resolveRequestMaxOutputTokens = resolve_request_max_output_tokens
resolveCompactionPolicy = resolve_compaction_policy
resolveCompactionRequestOutputTokens = resolve_compaction_request_output_tokens
resolveProviderProfileTokenLimits = resolve_provider_profile_token_limits
computeProviderTokenBudget = compute_provider_token_budget
