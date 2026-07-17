from __future__ import annotations

from .shared import *
from ..usage_costs import ensureUsageTotals as _ensure_usage_totals_shared
from ..usage_costs import mergeUsageIntoSession as _merge_usage_into_session_shared
from ..usage_costs import estimateUsageCostUsd as _estimate_usage_cost_shared
from ..usage_costs import roundUsd as _round_usd_shared
from ..usage_costs import resolveModelPricing as _resolve_pricing_shared

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
    return _estimate_usage_cost_shared(usage, model)

def _resolve_pricing(model: str):
    return _resolve_pricing_shared(model)

def _round_usd(value: float) -> float:
    return _round_usd_shared(value)
