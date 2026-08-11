"""Agent-facing prompt-cache orchestration: attach prepared context before a
provider call, commit the observation after it, and record eviction intent
at turn finalization."""
from __future__ import annotations

from ..runtime.events.emit import safe_emit
import hashlib
import json
from typing import Any
from .prompt_cache import resolvePromptCachePolicy
from .prompt_cache_state import markPromptCacheEvictionIntent
from ..runtime.paths import get_active_runtime_paths, resolve_runtime_paths
from ..runtime.events.emit import safe_emit as _emit
import copy
from .prompt_cache_state import (
    commitPromptCacheObservation,
    createPromptCacheDispatchOrder,
    readPromptCacheState,
)
from .usage_costs import cacheCreationUsageDetail


async def loadPreviousPromptCacheState(args: dict[str, Any]) -> dict[str, Any] | None:
    policy = resolvePromptCachePolicy(config=args.get("config") or {})
    session = args.get("session") or {}
    provider = session.get("provider") or (args.get("config") or {}).get("provider")
    if provider != "anthropic" or not policy.get("enabled"):
        return None
    return (await readPromptCacheState(
        {"sessionId": session.get("id"), "paths": args.get("paths")}
    )).get("state")


async def createPromptCacheTurnContext(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "previousState": await loadPreviousPromptCacheState(args),
        "compactionsBefore": int((args.get("session") or {}).get("metadata", {}).get("compactions") or 0),
    }


def attachPreparedPromptCacheContext(args: dict[str, Any]) -> dict[str, Any]:
    prepared = args["prepared"]
    continuity = prepared.get("continuity") or {}
    session = args["session"]
    preparation = resolvePromptCachePreparation(
        {
            "reactiveAttempt": args.get("reactiveAttempt"),
            "durableCompactionApplied": int(session.get("metadata", {}).get("compactions") or 0) > args["turnContext"]["compactionsBefore"],
            "continuationReliefApplied": bool((continuity.get("relief") or {}).get("applied")),
            "repaired": bool((args.get("repair") or {}).get("repaired") or (continuity.get("protocolRepair") or {}).get("changed")),
        }
    )
    prepared["request"] = attachPromptCacheDispatchContext(
        {
            "request": prepared["request"],
            "config": args["config"],
            "runtime": args["runtime"],
            "session": session,
            "paths": args["paths"],
            "reactiveAttempt": args.get("reactiveAttempt"),
            "preparation": preparation,
            "previousState": args["turnContext"].get("previousState"),
        }
    )
    prepared["promptCachePreparation"] = preparation
    return prepared


def attachPromptCacheDispatchContext(args: dict[str, Any]) -> dict[str, Any]:
    request = args["request"]
    config = args.get("config") or {}
    session = args.get("session") or {}
    provider = session.get("provider") or config.get("provider")
    policy = resolvePromptCachePolicy(config=config, request=request)
    if provider != "anthropic" or not policy.get("enabled"):
        return request
    previous = args.get("previousState")
    previous_anchor = (
        previous.get("request", {}).get("tailA") if isinstance(previous, dict) else None
    )
    policy["messageTailPreviousAnchor"] = copy.deepcopy(previous_anchor)
    binding = (args.get("runtime") or {}).get("sessionBinding") or {}
    policy["dispatchContext"] = {
        "sessionId": session.get("id"),
        "paths": args.get("paths"),
        "dispatchOrder": createPromptCacheDispatchOrder(
            {
                "runtimeGeneration": binding.get("generation"),
                "messageCount": len(request.get("messages") or []),
                "reactiveAttempt": args.get("reactiveAttempt", 0),
            }
        ),
        "preparation": args.get("preparation") or "normal",
    }
    if isinstance(previous, dict):
        policy["dispatchContext"]["previous"] = {
            "request": copy.deepcopy(previous.get("request")),
            "responseId": previous.get("response", {}).get("id"),
        }
    return {**request, "promptCache": policy}


def resolvePromptCachePreparation(args: dict[str, Any]) -> str:
    if args.get("repaired"):
        return "repaired"
    if int(args.get("reactiveAttempt") or 0) > 0:
        return "reactive_relief"
    if args.get("durableCompactionApplied") or args.get("continuationReliefApplied"):
        return "compacted"
    return "normal"


async def commitProviderPromptCacheObservation(
    response: dict[str, Any], runtime: dict[str, Any] | None = None
) -> None:
    observation = response.get("promptCache")
    if not isinstance(observation, dict):
        return
    result = await commitPromptCacheObservation(
        {**observation, "usage": response.get("usage") or {}}
    )
    if result["status"] == "unavailable":
        _emit(runtime, "provider.prompt_cache", {
            "session_id": observation.get("sessionId"),
            "phase": "state_error",
            "state_status": "unavailable",
        })
        return
    detail = cacheCreationUsageDetail(response.get("usage") or {})
    event = {
        "session_id": observation.get("sessionId"),
        "phase": "observed",
        "mode": observation.get("plan", {}).get("mode"),
        "ttl": observation.get("ttl"),
        "scope": observation.get("scope"),
        "used_breakpoints": observation.get("plan", {}).get("usedBreakpoints"),
        "placements": observation.get("plan", {}).get("placements"),
        "cache_read_input_tokens": (response.get("usage") or {}).get("cache_read_input_tokens"),
        "cache_creation_input_tokens": detail["aggregate"],
        "cache_creation_5m_input_tokens": detail["fiveMinutes"],
        "cache_creation_1h_input_tokens": detail["oneHour"],
        "cache_creation_ttl_detail_complete": detail["complete"],
        "provider_reason": observation.get("providerDiagnosis"),
        "state_status": result["status"],
    }
    if result["status"] == "committed":
        event["local_reason"] = result.get("state", {}).get("diagnosis", {}).get("localReason")
    _emit(runtime, "provider.prompt_cache", event)


_TERMINAL_SUCCESS_REASONS = {"end_turn", "stop", "completed"}


def isPromptCacheEvictionTerminalSuccess(args: dict[str, Any]) -> bool:
    return bool(
        not args.get("signal")
        and not args.get("error")
        and args.get("stopReason") in _TERMINAL_SUCCESS_REASONS
    )


async def recordPromptCacheEvictionIntentAtFinalization(args: dict[str, Any]) -> None:
    session = args.get("session") or {}
    config = args.get("config") or {}
    runtime = args.get("runtime") or {}
    try:
        provider = session.get("provider") or config.get("provider")
        if provider != "anthropic" or not isPromptCacheEvictionTerminalSuccess(args):
            return
        policy = resolvePromptCachePolicy(config=config)
        if not policy.get("enabled") or not policy.get("evictOnComplete"):
            return
        paths = (
            resolve_runtime_paths(candidate=config.get("runtimeHome"))
            if config.get("runtimeHome")
            else get_active_runtime_paths()
        )
        result = await markPromptCacheEvictionIntent(
            {
                "sessionId": session.get("id"),
                "paths": paths,
                "finalizationKey": _finalization_key(session, str(args.get("stopReason"))),
            }
        )
        if result["status"] == "unavailable":
            safe_emit(runtime, "provider.prompt_cache", {
                "session_id": session.get("id"), "phase": "state_error", "state_status": "unavailable"
            })
        elif result["status"] == "recorded":
            safe_emit(runtime, "provider.prompt_cache", {
                "session_id": session.get("id"),
                "phase": "eviction_intent",
                "mode": policy.get("mode"),
                "ttl": policy.get("ttl"),
                "scope": policy.get("scope"),
                "state_status": "recorded",
                "intent_status": "recorded",
            })
    except Exception:
        safe_emit(runtime, "provider.prompt_cache", {
            "session_id": session.get("id"), "phase": "state_error", "state_status": "unavailable"
        })


def _finalization_key(session: dict[str, Any], stop_reason: str) -> str:
    encoded = json.dumps(
        {
            "schemaVersion": 1,
            "sessionId": session.get("id"),
            "stopReason": stop_reason,
            "messageCount": len(session.get("messages") or []),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
