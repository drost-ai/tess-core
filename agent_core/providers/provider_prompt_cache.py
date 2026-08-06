from __future__ import annotations

import copy
from typing import Any

from .prompt_cache_state import (
    commitPromptCacheObservation,
    createPromptCacheDispatchOrder,
    readPromptCacheState,
)
from .prompt_cache import resolvePromptCachePolicy
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


def _emit(runtime: dict[str, Any] | None, event: str, data: dict[str, Any]) -> None:
    try:
        (runtime or {}).get("emitEvent", lambda *_: None)(event, data)
    except Exception:
        pass


load_previous_prompt_cache_state = loadPreviousPromptCacheState
attach_prompt_cache_dispatch_context = attachPromptCacheDispatchContext
resolve_prompt_cache_preparation = resolvePromptCachePreparation
commit_provider_prompt_cache_observation = commitProviderPromptCacheObservation
create_prompt_cache_turn_context = createPromptCacheTurnContext
attach_prepared_prompt_cache_context = attachPreparedPromptCacheContext
