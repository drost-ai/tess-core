from __future__ import annotations

from .shared import *
from ..usage_costs import ensureUsageTotals as _ensure_usage_totals_shared
from ..usage_costs import mergeUsageIntoSession as _merge_usage_into_session_shared

def _apply_plan_mode_overlay(base: str, session: dict[str, Any]) -> str:
    if session.get("permissionMode") != "plan":
        return base
    state = session.get("metadata", {}).get("planMode", {})
    return buildPlanModeSystemPrompt(base, {"currentState": state.get("state"), "priorPlan": state.get("plan")})

def _requires_plan_approval(session: dict[str, Any]) -> bool:
    return session.get("permissionMode") == "plan" and session.get("metadata", {}).get("planMode", {}).get("state") != "approved"

def _ensure_usage_totals(session: dict[str, Any]) -> dict[str, Any]:
    return _ensure_usage_totals_shared(session)

def _merge_usage_into_session(session: dict[str, Any], usage: dict[str, Any]) -> None:
    _merge_usage_into_session_shared(session, usage)

def _estimate_usage_cost(usage: dict[str, Any], model: str) -> float:
    pricing = _resolve_pricing(model)
    if not pricing:
        return 0.0
    return (
        (usage.get("input_tokens") or 0) * pricing[0]
        + (usage.get("output_tokens") or 0) * pricing[1]
        + (usage.get("cache_creation_input_tokens") or 0) * pricing[2]
        + (usage.get("cache_read_input_tokens") or 0) * pricing[3]
    ) / 1_000_000

def _resolve_pricing(model: str):
    key = model.strip().lower()
    if key in MODEL_PRICING:
        return MODEL_PRICING[key]
    if key.startswith("gpt-5.4"):
        return MODEL_PRICING["gpt-5.4"]
    if key.startswith("gpt-5-mini"):
        return MODEL_PRICING["gpt-5-mini-2025-08-07"]
    if "sonnet-4" in key:
        return MODEL_PRICING["claude-sonnet-4-6"]
    if "opus-4" in key:
        return MODEL_PRICING["claude-opus-4-6"]
    if "haiku-4-5" in key:
        return MODEL_PRICING["claude-haiku-4-5"]
    return None

def _round_usd(value: float) -> float:
    return round(value, 6)
