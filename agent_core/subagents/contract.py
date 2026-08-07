from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping


_ALIASES = {
    "maxBudgetUsd": ("maxBudgetUsd", "max_budget_usd"),
    "maxTurns": ("maxTurns", "max_turns"),
    "timeoutMs": ("timeoutMs", "timeout_ms"),
    "maxToolCalls": ("maxToolCalls", "max_tool_calls"),
    "responseSchema": ("responseSchema", "response_schema"),
    "concurrencyGroup": ("concurrencyGroup", "concurrency_group"),
}


def _first_present(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def _positive_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _string_list(values: Any) -> list[str] | None:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, bytearray)):
        return None
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out or None


def _communication_policy(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    policy: dict[str, Any] = {}
    if "allowChildQuestions" in value or "allow_child_questions" in value:
        policy["allowChildQuestions"] = bool(
            value.get("allowChildQuestions") if "allowChildQuestions" in value else value.get("allow_child_questions")
        )
    if "maxPendingQuestions" in value or "max_pending_questions" in value:
        parsed = _positive_int(
            value.get("maxPendingQuestions") if "maxPendingQuestions" in value else value.get("max_pending_questions")
        )
        if parsed is not None:
            policy["maxPendingQuestions"] = parsed
    return policy or None


def normalize_subagent_runtime_contract(definition: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize an agent definition into the persisted runtime contract.

    The persisted shape intentionally uses the TypeScript parity field names.
    Python-style aliases are accepted only at config input boundaries.
    """
    if not isinstance(definition, Mapping):
        return None
    contract: dict[str, Any] = {}
    tools = _string_list(definition.get("tools") or definition.get("allowedTools") or definition.get("allowed_tools"))
    if tools:
        contract["tools"] = tools
    max_budget = _positive_number(_first_present(definition, *_ALIASES["maxBudgetUsd"]))
    if max_budget is not None:
        contract["maxBudgetUsd"] = max_budget
    for key in ("maxTurns", "timeoutMs", "maxToolCalls"):
        parsed = _positive_int(_first_present(definition, *_ALIASES[key]))
        if parsed is not None:
            contract[key] = parsed
    response_schema = _first_present(definition, *_ALIASES["responseSchema"])
    if isinstance(response_schema, Mapping):
        contract["responseSchema"] = copy.deepcopy(dict(response_schema))
    communication = _communication_policy(definition.get("communication"))
    if communication:
        contract["communication"] = communication
    concurrency_group = _first_present(definition, *_ALIASES["concurrencyGroup"])
    if isinstance(concurrency_group, str) and concurrency_group.strip():
        contract["concurrencyGroup"] = concurrency_group.strip()
    if isinstance(definition.get("resident"), bool):
        contract["resident"] = definition["resident"]
    return contract or None


def runtime_contract_for_agent(config: Mapping[str, Any], agent_type: str | None) -> dict[str, Any] | None:
    if not agent_type:
        return None
    agents = config.get("agents")
    if not isinstance(agents, Mapping):
        return None
    definition = agents.get(agent_type)
    return normalize_subagent_runtime_contract(definition if isinstance(definition, Mapping) else None)


def summarize_subagent_runtime_contract(contract: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(contract, Mapping) or not contract:
        return None
    summary: dict[str, Any] = {}
    tools = _string_list(contract.get("tools"))
    if tools:
        summary["tools"] = tools
    if contract.get("maxBudgetUsd") is not None:
        summary["max_budget_usd"] = contract.get("maxBudgetUsd")
    if contract.get("maxTurns") is not None:
        summary["max_turns"] = contract.get("maxTurns")
    if contract.get("timeoutMs") is not None:
        summary["timeout_ms"] = contract.get("timeoutMs")
    if contract.get("maxToolCalls") is not None:
        summary["max_tool_calls"] = contract.get("maxToolCalls")
    if contract.get("concurrencyGroup") is not None:
        summary["concurrency_group"] = contract.get("concurrencyGroup")
    if isinstance(contract.get("communication"), Mapping):
        summary["communication"] = copy.deepcopy(dict(contract["communication"]))
    if isinstance(contract.get("responseSchema"), Mapping):
        summary["response_schema_configured"] = True
    return summary or None


def filter_tools_for_subagent(tools: Iterable[Any], contract: Mapping[str, Any] | None) -> list[Any]:
    if not isinstance(contract, Mapping):
        return list(tools or [])
    allowed = set(_string_list(contract.get("tools")) or [])
    if not allowed:
        return list(tools or [])
    return [tool for tool in (tools or []) if getattr(tool, "name", None) in allowed]


normalizeSubagentRuntimeContract = normalize_subagent_runtime_contract
runtimeContractForAgent = runtime_contract_for_agent
summarizeSubagentRuntimeContract = summarize_subagent_runtime_contract
filterToolsForSubagent = filter_tools_for_subagent


__all__ = [
    "filterToolsForSubagent",
    "filter_tools_for_subagent",
    "normalizeSubagentRuntimeContract",
    "normalize_subagent_runtime_contract",
    "runtimeContractForAgent",
    "runtime_contract_for_agent",
    "summarizeSubagentRuntimeContract",
    "summarize_subagent_runtime_contract",
]
