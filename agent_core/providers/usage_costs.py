from __future__ import annotations

from ._core import (
    finite_nonnegative_int as _finite_nonnegative,
    finite_nonnegative_int_or_none as _finite_optional,
)

import math
from typing import Any, Mapping


# Reviewed against Anthropic's public model pricing table on 2026-07-17.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"inputUsdPerMtok": 5, "outputUsdPerMtok": 25, "cacheWrite5mUsdPerMtok": 6.25, "cacheWrite1hUsdPerMtok": 10, "cacheReadUsdPerMtok": 0.5},
    "claude-opus-4-6": {"inputUsdPerMtok": 5, "outputUsdPerMtok": 25, "cacheWrite5mUsdPerMtok": 6.25, "cacheWrite1hUsdPerMtok": 10, "cacheReadUsdPerMtok": 0.5},
    "claude-sonnet-4-6": {"inputUsdPerMtok": 3, "outputUsdPerMtok": 15, "cacheWrite5mUsdPerMtok": 3.75, "cacheWrite1hUsdPerMtok": 6, "cacheReadUsdPerMtok": 0.3},
    "claude-haiku-4-5": {"inputUsdPerMtok": 1, "outputUsdPerMtok": 5, "cacheWrite5mUsdPerMtok": 1.25, "cacheWrite1hUsdPerMtok": 2, "cacheReadUsdPerMtok": 0.1},
    "claude-sonnet-4-5": {"inputUsdPerMtok": 3, "outputUsdPerMtok": 15, "cacheWrite5mUsdPerMtok": 3.75, "cacheWrite1hUsdPerMtok": 6, "cacheReadUsdPerMtok": 0.3},
    "claude-opus-4-5": {"inputUsdPerMtok": 5, "outputUsdPerMtok": 25, "cacheWrite5mUsdPerMtok": 6.25, "cacheWrite1hUsdPerMtok": 10, "cacheReadUsdPerMtok": 0.5},
    "gpt-5.4": {"inputUsdPerMtok": 2.5, "outputUsdPerMtok": 15, "cacheWrite5mUsdPerMtok": 0, "cacheWrite1hUsdPerMtok": 0, "cacheReadUsdPerMtok": 0},
    "gpt-5-mini-2025-08-07": {"inputUsdPerMtok": 0.25, "outputUsdPerMtok": 2, "cacheWrite5mUsdPerMtok": 0, "cacheWrite1hUsdPerMtok": 0, "cacheReadUsdPerMtok": 0},
}


def createUsageTotals() -> dict[str, Any]:
    return {
        "turns": 0,
        "toolCalls": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheCreationInputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreation5mInputTokens": 0,
        "cacheCreation1hInputTokens": 0,
        "cacheCreationTtlDetailComplete": True,
    }


def ensureUsageTotals(session: dict[str, Any]) -> dict[str, Any]:
    metadata = session.setdefault("metadata", {})
    usage = metadata.get("usage")
    if not isinstance(usage, dict):
        usage = createUsageTotals()
        metadata["usage"] = usage
        return usage
    legacy_detail = any(
        field not in usage
        for field in (
            "cacheCreation5mInputTokens",
            "cacheCreation1hInputTokens",
            "cacheCreationTtlDetailComplete",
        )
    )
    for field in (
        "turns",
        "toolCalls",
        "inputTokens",
        "outputTokens",
        "cacheCreationInputTokens",
        "cacheReadInputTokens",
        "cacheCreation5mInputTokens",
        "cacheCreation1hInputTokens",
    ):
        usage[field] = _finite_nonnegative(usage.get(field))
    usage["cacheCreationTtlDetailComplete"] = (
        False if legacy_detail else usage.get("cacheCreationTtlDetailComplete") is True
    )
    return usage


def cacheCreationUsageDetail(usage: Mapping[str, Any]) -> dict[str, Any]:
    aggregate = _finite_nonnegative(usage.get("cache_creation_input_tokens"))
    five_present = _finite_optional(usage.get("cache_creation_5m_input_tokens")) is not None
    one_hour_present = _finite_optional(usage.get("cache_creation_1h_input_tokens")) is not None
    five_minutes = _finite_nonnegative(usage.get("cache_creation_5m_input_tokens"))
    one_hour = _finite_nonnegative(usage.get("cache_creation_1h_input_tokens"))
    present = five_present or one_hour_present
    reconciled = five_minutes + one_hour == aggregate
    structurally_complete = reconciled and (present or aggregate == 0)
    return {
        "aggregate": aggregate,
        "fiveMinutes": five_minutes,
        "oneHour": one_hour,
        "present": present,
        "reconciled": reconciled,
        "complete": usage.get("cache_creation_ttl_detail_complete") is not False
        and structurally_complete,
    }


def mergeProviderUsage(target: dict[str, Any], nextUsage: Mapping[str, Any]) -> None:
    target_complete = cacheCreationUsageDetail(target)["complete"]
    next_complete = cacheCreationUsageDetail(nextUsage)["complete"]
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_creation_5m_input_tokens",
        "cache_creation_1h_input_tokens",
        "cache_read_input_tokens",
    ):
        target[field] = _finite_nonnegative(target.get(field)) + _finite_nonnegative(
            nextUsage.get(field)
        )
    target["cache_creation_ttl_detail_complete"] = target_complete and next_complete


def mergeUsageIntoSession(session: dict[str, Any], usage: Mapping[str, Any]) -> None:
    totals = ensureUsageTotals(session)
    totals["inputTokens"] += _finite_nonnegative(usage.get("input_tokens"))
    totals["outputTokens"] += _finite_nonnegative(usage.get("output_tokens"))
    totals["cacheCreationInputTokens"] += _finite_nonnegative(
        usage.get("cache_creation_input_tokens")
    )
    totals["cacheReadInputTokens"] += _finite_nonnegative(usage.get("cache_read_input_tokens"))
    totals["cacheCreation5mInputTokens"] += _finite_nonnegative(
        usage.get("cache_creation_5m_input_tokens")
    )
    totals["cacheCreation1hInputTokens"] += _finite_nonnegative(
        usage.get("cache_creation_1h_input_tokens")
    )
    totals["cacheCreationTtlDetailComplete"] = (
        totals.get("cacheCreationTtlDetailComplete") is True
        and cacheCreationUsageDetail(usage)["complete"]
    )


def resolveModelPricing(model: str) -> dict[str, float] | None:
    key = str(model or "").strip().lower()
    if key in MODEL_PRICING:
        return MODEL_PRICING[key]
    aliases = (
        ("opus-4-8", "claude-opus-4-8"),
        ("opus-4-6", "claude-opus-4-6"),
        ("sonnet-4-6", "claude-sonnet-4-6"),
        ("haiku-4-5", "claude-haiku-4-5"),
        ("sonnet-4-5", "claude-sonnet-4-5"),
        ("opus-4-5", "claude-opus-4-5"),
        ("sonnet-4", "claude-sonnet-4-6"),
        ("opus-4", "claude-opus-4-6"),
    )
    for needle, canonical in aliases:
        if needle in key:
            return MODEL_PRICING[canonical]
    if key.startswith("gpt-5.4"):
        return MODEL_PRICING["gpt-5.4"]
    if key.startswith("gpt-5-mini"):
        return MODEL_PRICING["gpt-5-mini-2025-08-07"]
    return None


def estimateUsageCost(usage: Mapping[str, Any], model: str) -> dict[str, Any]:
    detail = cacheCreationUsageDetail(usage)
    pricing = resolveModelPricing(model)
    if pricing is None:
        return {
            "totalUsd": 0.0,
            "cacheWriteUsd": 0.0,
            "cacheWriteEstimated": False,
            "cacheCreationTtlDetailComplete": detail["complete"],
        }
    input_cost = _finite_nonnegative(usage.get("input_tokens")) * pricing["inputUsdPerMtok"] / 1_000_000
    output_cost = _finite_nonnegative(usage.get("output_tokens")) * pricing["outputUsdPerMtok"] / 1_000_000
    if detail["complete"]:
        cache_write = (
            detail["fiveMinutes"] * pricing["cacheWrite5mUsdPerMtok"]
            + detail["oneHour"] * pricing["cacheWrite1hUsdPerMtok"]
        ) / 1_000_000
    else:
        cache_write = detail["aggregate"] * pricing["cacheWrite5mUsdPerMtok"] / 1_000_000
    cache_read = _finite_nonnegative(usage.get("cache_read_input_tokens")) * pricing["cacheReadUsdPerMtok"] / 1_000_000
    return {
        "totalUsd": input_cost + output_cost + cache_write + cache_read,
        "cacheWriteUsd": cache_write,
        "cacheWriteEstimated": not detail["complete"] and detail["aggregate"] > 0,
        "cacheCreationTtlDetailComplete": detail["complete"],
    }


def estimateUsageCostUsd(usage: Mapping[str, Any], model: str) -> float:
    return float(estimateUsageCost(usage, model)["totalUsd"])


def roundUsd(value: float) -> float:
    """Round USD values with JavaScript ``Math.round`` semantics."""
    numeric = float(value)
    if not math.isfinite(numeric):
        return numeric
    return math.floor(numeric * 1_000_000 + 0.5) / 1_000_000




