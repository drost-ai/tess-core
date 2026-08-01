from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_core.providers.anthropic_cache import planAnthropicCache

CACHE_ECONOMICS_SCHEMA = "agent-core.cache-economics.v1"
TTLS = ("5m", "1h")
STAGES = ("warmup", "steady", "compaction", "relief", "fork", "resume")
_WEIGHTS = {"automatic": 1_000, "system": 2_500, "tools": 1_500, "tail_a": 5_000, "tail_b": 3_500}
_RECORDED_READ_TOKENS = {
    "warmup": 0,
    "steady": 7_500,
    "compaction": 4_000,
    "relief": 7_500,
    "fork": 4_000,
    "resume": 7_500,
}


def runCacheEconomicLane(*, seed_wasted_slot: bool = False) -> dict[str, Any]:
    ttl_reports = [_run_ttl(ttl, seed_wasted_slot=seed_wasted_slot) for ttl in TTLS]
    regressions = [
        regression
        for report in ttl_reports
        for regression in report["regressions"]
    ]
    return {
        "schemaVersion": CACHE_ECONOMICS_SCHEMA,
        "status": "failed" if regressions else "passed",
        "measurementPolicy": "offline fake usage; production four-slot plan; no provider calls",
        "recordedCacheReadTokens": dict(_RECORDED_READ_TOKENS),
        "seededWastedSlot": seed_wasted_slot,
        "ttls": ttl_reports,
        "regressions": regressions,
    }


def _run_ttl(ttl: str, *, seed_wasted_slot: bool) -> dict[str, Any]:
    registry: set[str] = set()
    previous_anchor: dict[str, Any] | None = None
    previous_ids: set[str] = set()
    observations = []
    regressions = []
    for stage, messages in _scripted_messages():
        plan = planAnthropicCache(
            {
                "policy": _policy(ttl),
                "normalizedSystem": _system(),
                "normalizedTools": _tools(),
                "normalizedMessages": messages,
                "previousAnchor": previous_anchor,
            }
        )
        identities = _breakpoint_identities(plan)
        if seed_wasted_slot and stage == "relief":
            registry.discard(_identity("system", _system()))
        hits = [placement for placement, identity in identities.items() if identity in registry]
        read_tokens = min(12_000, sum(_WEIGHTS[placement] for placement in hits))
        creation_tokens = min(
            12_000,
            sum(_WEIGHTS[placement] for placement in identities if placement not in hits),
        )
        usage = {
            "input_tokens": 12_000,
            "cache_read_input_tokens": read_tokens,
            "cache_creation_input_tokens": creation_tokens,
            f"cache_creation_{'5m' if ttl == '5m' else '1h'}_input_tokens": creation_tokens,
            "cache_creation_ttl_detail_complete": True,
        }
        lost_slots = len(previous_ids - set(identities.values()))
        observation = {
            "stage": stage,
            "placements": list(plan["placements"]),
            "usedBreakpoints": plan["usedBreakpoints"],
            "omitted": plan["omitted"],
            "cacheHitPlacements": hits,
            "cacheReadRatio": round(read_tokens / usage["input_tokens"], 4),
            "lostSlotsFromPreviousRequest": lost_slots,
            "usage": usage,
        }
        observations.append(observation)
        expected_read = _RECORDED_READ_TOKENS[stage]
        if read_tokens < expected_read:
            regressions.append(
                {
                    "metric": f"prompt_cache.{ttl}.{stage}.cache_read_input_tokens",
                    "measured": read_tokens,
                    "budget": expected_read,
                    "ownerPackage": 47,
                    "verdict": "performance.regressed",
                }
            )
        if plan["usedBreakpoints"] > 4:
            regressions.append(
                {
                    "metric": f"prompt_cache.{ttl}.{stage}.used_breakpoints",
                    "measured": plan["usedBreakpoints"],
                    "budget": 4,
                    "ownerPackage": 32,
                    "verdict": "performance.regressed",
                }
            )
        registry.update(identities.values())
        previous_ids = set(identities.values())
        previous_anchor = plan.get("tailA")
    return {"ttl": ttl, "status": "failed" if regressions else "passed", "observations": observations, "regressions": regressions}


def _breakpoint_identities(plan: dict[str, Any]) -> dict[str, str]:
    identities: dict[str, str] = {}
    if "automatic" in plan["placements"]:
        identities["automatic"] = "automatic"
    if plan.get("system"):
        identities["system"] = _identity("system", _system())
    if plan.get("tools"):
        identities["tools"] = _identity("tools", _tools())
    for placement, key in (("tail_a", "tailA"), ("tail_b", "tailB")):
        anchor = plan.get(key)
        if isinstance(anchor, dict) and isinstance(anchor.get("identity"), str):
            identities[placement] = anchor["identity"]
    return identities


def _scripted_messages() -> list[tuple[str, list[dict[str, Any]]]]:
    initial = _messages("main", 4)
    steady = initial + _messages("steady", 2, start_role="user")
    compacted = [
        {"role": "user", "content": [{"type": "text", "text": "checkpoint summary"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "checkpoint accepted"}]},
        *_messages("post-compact", 2),
    ]
    relief = compacted + _messages("relief", 2)
    fork = steady + _messages("fork", 2)
    resume = fork + _messages("resume", 2)
    return [
        ("warmup", initial),
        ("steady", steady),
        ("compaction", compacted),
        ("relief", relief),
        ("fork", fork),
        ("resume", resume),
    ]


def _messages(prefix: str, count: int, *, start_role: str = "user") -> list[dict[str, Any]]:
    first_user = start_role == "user"
    return [
        {
            "role": "user" if (index % 2 == 0) == first_user else "assistant",
            "content": [{"type": "text", "text": f"{prefix}-{index}"}],
        }
        for index in range(count)
    ]


def _system() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "stable system identity"}]


def _tools() -> list[dict[str, Any]]:
    return [{"name": "Read", "description": "Read a file", "input_schema": {"type": "object"}}]


def _policy(ttl: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "mode": "explicit",
        "explicit": True,
        "ttl": ttl,
        "toolDefinitionBreakpoint": True,
        "messageTailBreakpoints": True,
    }


def _identity(prefix: str, value: Any) -> str:
    serialized = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(serialized).hexdigest()}"


run_cache_economic_lane = runCacheEconomicLane
