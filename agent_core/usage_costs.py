from __future__ import annotations

import math
from typing import Any, Mapping


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


def _finite_optional(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _finite_nonnegative(value: Any) -> int:
    return _finite_optional(value) or 0


create_usage_totals = createUsageTotals
ensure_usage_totals = ensureUsageTotals
cache_creation_usage_detail = cacheCreationUsageDetail
merge_provider_usage = mergeProviderUsage
merge_usage_into_session = mergeUsageIntoSession
