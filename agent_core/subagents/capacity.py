from __future__ import annotations

import json
import os
from typing import Any, Iterable, Mapping


ACTIVE_TASK_STATUSES = {"queued", "running", "backgrounded", "awaiting_input", "reconciling", "degraded"}


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def resolve_subagent_concurrency_policy(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = env or os.environ
    default_limit = _positive_int(source.get("TESS_SUBAGENT_MAX_CONCURRENCY")) or _positive_int(
        source.get("AGENT_CORE_SUBAGENT_MAX_CONCURRENCY")
    )
    if default_limit is None:
        # Call-time import: the config package imports subagents at module load.
        from ..config._knobs import knobValue

        default_limit = _positive_int(knobValue("subagents.max_live"))
    groups: dict[str, int] = {}
    raw_groups = source.get("TESS_SUBAGENT_CONCURRENCY_GROUP_LIMITS") or source.get(
        "AGENT_CORE_SUBAGENT_CONCURRENCY_GROUP_LIMITS"
    )
    if raw_groups:
        try:
            decoded = json.loads(raw_groups)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, Mapping):
            for key, value in decoded.items():
                parsed = _positive_int(value)
                if isinstance(key, str) and key.strip() and parsed is not None:
                    groups[key.strip()] = parsed
        else:
            for item in raw_groups.split(","):
                if "=" not in item:
                    continue
                key, value = item.split("=", 1)
                parsed = _positive_int(value)
                if key.strip() and parsed is not None:
                    groups[key.strip()] = parsed
    return {"maxConcurrency": default_limit, "groupLimits": groups}


def _active_subagent_records(records: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        record
        for record in records
        if record.get("kind") == "subagent" and record.get("status") in ACTIVE_TASK_STATUSES
    ]


def check_subagent_capacity(
    policy: Mapping[str, Any],
    task_records: Iterable[Mapping[str, Any]],
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    active = _active_subagent_records(task_records)
    limit = _positive_int(policy.get("maxConcurrency"))
    if limit is not None and len(active) >= limit:
        return {"reason": "max_concurrency", "limit": limit, "active": len(active)}
    group = contract.get("concurrencyGroup") if isinstance(contract, Mapping) else None
    group_limits = policy.get("groupLimits") if isinstance(policy.get("groupLimits"), Mapping) else {}
    group_limit = _positive_int(group_limits.get(group)) if isinstance(group, str) else None
    if group_limit is not None:
        active_group = [
            record
            for record in active
            if isinstance(record.get("metadata"), Mapping)
            and (record["metadata"].get("concurrency_group") or record["metadata"].get("concurrencyGroup")) == group
        ]
        if len(active_group) >= group_limit:
            return {"reason": "concurrency_group", "group": group, "limit": group_limit, "active": len(active_group)}
    return None


checkSubagentCapacity = check_subagent_capacity
resolveSubagentConcurrencyPolicy = resolve_subagent_concurrency_policy


__all__ = [
    "ACTIVE_TASK_STATUSES",
    "checkSubagentCapacity",
    "check_subagent_capacity",
    "resolveSubagentConcurrencyPolicy",
    "resolve_subagent_concurrency_policy",
]
