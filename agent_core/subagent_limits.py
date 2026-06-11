from __future__ import annotations

from typing import Any, Mapping


def resolve_subagent_runtime_limits(session: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), Mapping) else {}
    contract = metadata.get("subagentRuntimeContract") if isinstance(metadata, Mapping) else None
    if not isinstance(contract, Mapping):
        return None
    limits: dict[str, Any] = {}
    for key in ("maxTurns", "timeoutMs", "maxToolCalls"):
        if contract.get(key) is not None:
            limits[key] = int(contract[key])
    max_budget = contract.get("maxBudgetUsd")
    config_budget = config.get("maxBudgetUsd") or config.get("max_budget_usd")
    budgets = []
    for value in (max_budget, config_budget):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            budgets.append(parsed)
    if budgets:
        limits["maxBudgetUsd"] = min(budgets)
    return limits or None


def check_subagent_runtime_limits(limits: Mapping[str, Any] | None, state: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(limits, Mapping):
        return None
    if limits.get("maxTurns") is not None and int(state.get("providerCalls") or 0) >= int(limits["maxTurns"]):
        return {"reason": "max_turns", "limit": int(limits["maxTurns"])}
    if limits.get("maxToolCalls") is not None and int(state.get("toolCalls") or 0) >= int(limits["maxToolCalls"]):
        return {"reason": "max_tool_calls", "limit": int(limits["maxToolCalls"])}
    if limits.get("timeoutMs") is not None and int(state.get("elapsedMs") or 0) >= int(limits["timeoutMs"]):
        return {"reason": "timeout_ms", "limit": int(limits["timeoutMs"])}
    if limits.get("maxBudgetUsd") is not None and float(state.get("costUsd") or 0) >= float(limits["maxBudgetUsd"]):
        return {"reason": "max_budget_usd", "limit": float(limits["maxBudgetUsd"])}
    return None


def build_subagent_tool_limit_result(tool_use: Mapping[str, Any], exceeded: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use.get("id"),
        "is_error": True,
        "content": f"Subagent runtime limit exceeded: {exceeded.get('reason')} ({exceeded.get('limit')}).",
    }


resolveSubagentRuntimeLimits = resolve_subagent_runtime_limits
checkSubagentRuntimeLimits = check_subagent_runtime_limits
buildSubagentToolLimitResult = build_subagent_tool_limit_result


__all__ = [
    "buildSubagentToolLimitResult",
    "build_subagent_tool_limit_result",
    "checkSubagentRuntimeLimits",
    "check_subagent_runtime_limits",
    "resolveSubagentRuntimeLimits",
    "resolve_subagent_runtime_limits",
]
