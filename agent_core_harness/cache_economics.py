from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import agent_core as core
from agent_core.providers.turn_session import createProviderTurnSession

from .fake_providers.server import FakeProviderHttpServer

CACHE_ECONOMICS_SCHEMA = "agent-core.cache-economics.v2"
TTLS = ("5m", "1h")
STAGES = ("warmup", "steady", "compaction", "relief", "fork", "resume")
CACHE_READ_RATIO_BUDGETS = {
    "warmup": 0.0,
    "steady": 0.25,
    "compaction": 0.05,
    "relief": 0.05,
    "fork": 0.25,
    "resume": 0.25,
}


class _CacheAwareAnthropicLedger:
    def __init__(self, ttl: str, *, seed_wasted_slot: bool) -> None:
        self.ttl = ttl
        self.seed_wasted_slot = seed_wasted_slot
        self.registry: set[str] = set()
        self.observations: list[dict[str, Any]] = []

    def response(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = request.get("body") if isinstance(request.get("body"), dict) else {}
        headers = request.get("headers") if isinstance(request.get("headers"), dict) else {}
        stage = str(headers.get("x-cache-stage") or "unknown")
        if self.seed_wasted_slot and stage == "relief":
            self.registry.clear()
        breakpoints = _provider_breakpoints(payload)
        hits = [item for item in breakpoints if item["identity"] in self.registry]
        input_tokens = _token_estimate(payload)
        cache_read_tokens = max((int(item["prefixTokens"]) for item in hits), default=0)
        cache_creation_tokens = max(0, input_tokens - cache_read_tokens) if breakpoints else 0
        self.registry.update(str(item["identity"]) for item in breakpoints)
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": 2,
            "cache_read_input_tokens": cache_read_tokens,
            "cache_creation_input_tokens": cache_creation_tokens,
            "cache_creation": {
                "ephemeral_5m_input_tokens": cache_creation_tokens if self.ttl == "5m" else 0,
                "ephemeral_1h_input_tokens": cache_creation_tokens if self.ttl == "1h" else 0,
            },
        }
        self.observations.append(
            {
                "stage": stage,
                "breakpointCount": len(breakpoints),
                "breakpointIdentities": [item["identity"] for item in breakpoints],
                "hitIdentities": [item["identity"] for item in hits],
                "usage": copy.deepcopy(usage),
            }
        )
        return {
            "body": {
                "id": f"cache-{self.ttl}-{stage}",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": f"{stage} complete"}],
                "usage": usage,
                "stop_reason": "end_turn",
            }
        }


def runCacheEconomicLane(*, seed_wasted_slot: bool = False) -> dict[str, Any]:
    return asyncio.run(_run_cache_economic_lane(seed_wasted_slot=seed_wasted_slot))


async def _run_cache_economic_lane(*, seed_wasted_slot: bool) -> dict[str, Any]:
    ttl_reports = [
        await _run_ttl(ttl, seed_wasted_slot=seed_wasted_slot)
        for ttl in TTLS
    ]
    regressions = [
        regression
        for report in ttl_reports
        for regression in report["regressions"]
    ]
    return {
        "schemaVersion": CACHE_ECONOMICS_SCHEMA,
        "status": "failed" if regressions else "passed",
        "measurementPolicy": (
            "production Anthropic request planning and usage parsing over a loopback "
            "cache-aware provider; compaction, relief, fork, and resume use runtime functions"
        ),
        "providerCalls": sum(report["providerCalls"] for report in ttl_reports),
        "cacheReadRatioBudgets": dict(CACHE_READ_RATIO_BUDGETS),
        "seededWastedSlot": seed_wasted_slot,
        "ttls": ttl_reports,
        "regressions": regressions,
    }


async def _run_ttl(ttl: str, *, seed_wasted_slot: bool) -> dict[str, Any]:
    ledger = _CacheAwareAnthropicLedger(ttl, seed_wasted_slot=seed_wasted_slot)
    requests: list[dict[str, Any]] = []
    server = FakeProviderHttpServer(
        provider="anthropic",
        script={"messages": [{"responseFactory": ledger.response}]},
        request_recorder=requests,
        record_all_headers=True,
    ).start()
    runtime = {"emitEvent": lambda *_: None}
    config = {
        "provider": "anthropic",
        "anthropicBaseUrl": server.base_url,
        "apiKey": "package47-cache",
        "customHeaders": {},
        "providerCapabilities": {"anthropicHttpKeepAlive": True},
        "promptCache": {
            "enabled": True,
            "mode": "explicit",
            "explicit": True,
            "compatibility": "official",
            "ttl": ttl,
            "toolDefinitionBreakpoint": True,
            "messageTailBreakpoints": True,
        },
    }
    turn = createProviderTurnSession(
        logical_turn_id=f"package47-cache-{ttl}",
        session={"id": f"package47-cache-{ttl}", "provider": "anthropic"},
        config=config,
        runtime=runtime,
        model="claude-sonnet-4-6",
    )
    observations: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    previous_anchor: dict[str, Any] | None = None
    try:
        for stage, messages in _runtime_lifecycle_messages():
            stage_config = {
                **config,
                "customHeaders": {
                    "x-cache-stage": stage,
                    "x-seed-wasted-slot": "true" if seed_wasted_slot and stage == "relief" else "false",
                },
            }
            request = {
                "model": "claude-sonnet-4-6",
                "system": "stable system identity " * 200,
                "messages": copy.deepcopy(messages),
                "tools": [_tool_definition()],
                "promptCache": {
                    **stage_config["promptCache"],
                    "messageTailPreviousAnchor": previous_anchor,
                },
            }
            response = await core.sendAnthropicMessagesRequest(
                config=stage_config,
                request=request,
                runtime=runtime,
                maxAttempts=1,
                turnSession=turn,
            )
            provider_observation = ledger.observations[-1]
            cache = response.get("promptCache") if isinstance(response.get("promptCache"), dict) else {}
            plan = cache.get("plan") if isinstance(cache.get("plan"), dict) else {}
            previous_anchor = plan.get("tailA") if isinstance(plan.get("tailA"), dict) else None
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            read_tokens = int(usage.get("cache_read_input_tokens") or 0)
            input_tokens = max(1, int(usage.get("input_tokens") or 0))
            ratio = read_tokens / input_tokens
            observation = {
                "stage": stage,
                "runtimeLifecycle": True,
                # The provider-visible request is the economic authority. Some
                # adapters do not echo their internal plan in the response,
                # so report the breakpoints actually received on the wire.
                "usedBreakpoints": provider_observation["breakpointCount"],
                "placements": list(provider_observation["breakpointIdentities"]),
                "providerBreakpointCount": provider_observation["breakpointCount"],
                "providerHitCount": len(provider_observation["hitIdentities"]),
                "cacheReadRatio": round(ratio, 4),
                "usage": copy.deepcopy(usage),
            }
            observations.append(observation)
            budget = CACHE_READ_RATIO_BUDGETS[stage]
            if ratio + 1e-12 < budget:
                regressions.append(
                    {
                        "metric": f"prompt_cache.{ttl}.{stage}.cache_read_ratio",
                        "measured": round(ratio, 6),
                        "budget": budget,
                        "ownerPackage": 47,
                        "verdict": "performance.regressed",
                    }
                )
            if observation["usedBreakpoints"] > 4:
                regressions.append(
                    {
                        "metric": f"prompt_cache.{ttl}.{stage}.used_breakpoints",
                        "measured": observation["usedBreakpoints"],
                        "budget": 4,
                        "ownerPackage": 32,
                        "verdict": "performance.regressed",
                    }
                )
            detail_key = f"cache_creation_{ttl}_input_tokens"
            if usage.get("cache_creation_ttl_detail_complete") is not True or detail_key not in usage:
                regressions.append(
                    {
                        "metric": f"prompt_cache.{ttl}.{stage}.ttl_usage_complete",
                        "measured": False,
                        "budget": True,
                        "ownerPackage": 47,
                        "verdict": "performance.regressed",
                    }
                )
    finally:
        await turn.close()
        server.close()
    return {
        "ttl": ttl,
        "status": "failed" if regressions else "passed",
        "providerCalls": len(requests),
        "observations": observations,
        "regressions": regressions,
    }


def _runtime_lifecycle_messages() -> list[tuple[str, list[dict[str, Any]]]]:
    initial = _messages("warmup", 24)
    steady = [*copy.deepcopy(initial), *_messages("steady", 2)]
    compacted = core.apply_partial_compaction(copy.deepcopy(steady), 0)
    if not compacted.get("applied"):
        raise RuntimeError("Cache lane compaction scenario did not compact")
    compaction_messages = compacted["messages"]
    relief_source = [*copy.deepcopy(compaction_messages), *_messages("relief-source", 16)]
    relief_session = {"id": "cache-relief", "metadata": {}}
    relieved = core.apply_lightweight_context_relief(relief_source, relief_session)
    if not relieved.get("summary", {}).get("applied"):
        raise RuntimeError("Cache lane relief scenario did not apply")
    parent = {
        "id": "cache-parent",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "messages": copy.deepcopy(steady),
        "metadata": {},
    }
    forked = core.createForkedSession(
        parent=parent,
        id="cache-fork",
        cwd="/tmp/cache-fork",
        originalCwd="/tmp/cache-fork",
        now="2026-08-01T00:00:00+00:00",
        model="claude-sonnet-4-6",
        systemPrompt="stable system identity",
        permissionMode="default",
    )
    fork_messages = forked["messages"]
    resumed = [*copy.deepcopy(fork_messages), *_messages("resume", 2)]
    return [
        ("warmup", initial),
        ("steady", steady),
        ("compaction", compaction_messages),
        ("relief", relieved["messages"]),
        ("fork", fork_messages),
        ("resume", resumed),
    ]


def _provider_breakpoints(payload: dict[str, Any]) -> list[dict[str, Any]]:
    breakpoints: list[dict[str, Any]] = []
    prefix: dict[str, Any] = {"system": [], "tools": [], "messages": []}
    for section in ("system", "tools", "messages"):
        values = payload.get(section) if isinstance(payload.get(section), list) else []
        for value in values:
            clean = _without_cache_control(value)
            prefix[section].append(clean)
            if isinstance(value, dict) and isinstance(value.get("cache_control"), dict):
                breakpoints.append(_prefix_identity(prefix, value["cache_control"]))
            if isinstance(value, dict) and isinstance(value.get("content"), list):
                for block in value["content"]:
                    if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
                        breakpoints.append(_prefix_identity(prefix, block["cache_control"]))
    if isinstance(payload.get("cache_control"), dict):
        breakpoints.append(_prefix_identity(prefix, payload["cache_control"]))
    return breakpoints


def _prefix_identity(prefix: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(prefix, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    identity = hashlib.sha256(serialized).hexdigest()
    return {
        "identity": identity,
        "prefixTokens": max(1, len(serialized) // 4),
        "ttl": control.get("ttl") or "provider_default",
    }


def _without_cache_control(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_cache_control(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        str(key): _without_cache_control(item)
        for key, item in value.items()
        if key != "cache_control"
    }


def _token_estimate(payload: dict[str, Any]) -> int:
    return max(1, len(json.dumps(_without_cache_control(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")) // 4)


def _messages(prefix: str, count: int) -> list[dict[str, Any]]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"{prefix}-{index}:" + (" evidence" * 40),
        }
        for index in range(count)
    ]


def _tool_definition() -> dict[str, Any]:
    return {
        "name": "Read",
        "description": "Read a file using a stable definition " * 80,
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
        },
    }


run_cache_economic_lane = runCacheEconomicLane
