from __future__ import annotations

import asyncio
import copy
import re
from typing import Any

from ._utils import maybe_await, merge_usage
from .config import (
    buildPlanModeSystemPrompt,
    ensureProviderForModelSelection,
    filterToolsByPolicy,
    normalizeEffortForModel,
    resolveConfig,
    resolveFallbackProviderProfileName,
)
from .compaction import runCompaction, shouldAutoCompact
from .continuation import buildContinuationContext
from .provider_errors import fallbackEligibleProviderFailure, isProviderRequestError
from .providers.anthropic import sendAnthropicMessagesRequest
from .providers.openai import sendOpenAiRequest
from .session import persistProviderFailure, persistProviderRecovery, persistSession, repairSessionTranscript
from .token_accounting import measureRequestInputTokens, recordActualTokenUsage, recordPreflightTokenMeasurement


MODEL_PRICING = {
    "claude-opus-4-6": (5, 25, 6.25, 0.5),
    "claude-sonnet-4-6": (3, 15, 3.75, 0.3),
    "claude-haiku-4-5": (1, 5, 1.25, 0.1),
    "gpt-5.4": (2.5, 15, 0, 0),
    "gpt-5-mini-2025-08-07": (0.25, 2, 0, 0),
}

PLAN_SAFE_TOOL_NAMES = {
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "SetStatus",
    "Agent",
    "AskUserQuestion",
    "Config",
    "ExitPlanMode",
    "TaskMonitorStart",
    "TaskMonitorStop",
    "Mcp",
    "ListMcpResources",
    "ReadMcpResource",
}


async def runAgentPrompt(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    config = args["config"]
    prompt = args["prompt"]
    runtime = args["runtime"]
    provider_override = args.get("providerOverride")
    repair = repairSessionTranscript(session)
    if repair["repaired"]:
        await persistSession(session, config)
        _safe_emit(runtime, "transcript.repaired", {"session_id": session["id"], "truncated_from": repair.get("truncatedFrom"), "removed_count": repair["removedCount"], "missing_tool_use_ids": repair["missingToolUseIds"]})

    effective_provider = ensureProviderForModelSelection(session.get("provider") or config["provider"], args.get("modelOverride") or session["model"], config["provider"])
    session["provider"] = effective_provider
    provider_profile_name = resolveFallbackProviderProfileName(effective_provider, config.get("providerProfiles", {}), session.get("providerProfileName") or config.get("providerProfileName"))
    session["providerProfileName"] = provider_profile_name

    tool_names = set(filterToolsByPolicy(config))
    tool_registry = [tool for tool in runtime.get("tools", []) if tool.name in tool_names]
    provider = provider_override or _create_provider(config, runtime)
    base_system_prompt = args.get("systemPromptOverride") or session["systemPrompt"]
    text_parts: list[str] = []
    total_usage: dict[str, Any] = {}
    usage_totals = _ensure_usage_totals(session)
    active_model = args.get("modelOverride") or session["model"]
    cost = 0.0
    tool_count = 0
    last_stable_snapshot = copy.deepcopy(session)

    async def cancel_turn() -> dict[str, Any]:
        _restore_session_state(session, last_stable_snapshot)
        await persistSession(session, config)
        _safe_emit(runtime, "turn.cancelled", {"session_id": session["id"], "model": active_model, "partial_text": "\n\n".join(text_parts).strip()})
        return {"text": "\n\n".join(text_parts).strip(), "toolCount": tool_count, "usage": total_usage, "model": active_model, "estimatedCostUsd": _round_usd(cost), "stopReason": "cancelled"}

    if _is_turn_cancelled(runtime):
        return await cancel_turn()

    if prompt.strip():
        session.setdefault("messages", []).append({"role": "user", "content": prompt})
        await persistSession(session, config)
        _safe_emit(runtime, "message.user", {"text": prompt, "session_id": session["id"]})

    usage_totals["turns"] += 1

    while True:
        if _is_turn_cancelled(runtime):
            return await cancel_turn()
        try:
            prepared_request = await prepareProviderRequest(
                {
                    "session": session,
                    "config": config,
                    "runtime": runtime,
                    "baseSystemPrompt": base_system_prompt,
                    "toolRegistry": tool_registry,
                    "activeModel": active_model,
                    "prompt": prompt,
                    "reactiveAttempt": 0,
                }
            )
        except Exception as error:
            if _is_turn_abort_error(error, runtime.get("abortSignal")):
                return await cancel_turn()
            raise
        while True:
            try:
                _throw_if_turn_cancelled(runtime)
                response = await maybe_await(provider["send"](prepared_request["request"]))
                break
            except Exception as error:
                if _is_turn_abort_error(error, runtime.get("abortSignal")):
                    return await cancel_turn()
                if prepared_request.get("reactiveAttempt", 0) < 2 and isContextWindowFailure(error):
                    try:
                        prepared_request = await prepareProviderRequest(
                            {
                                "session": session,
                                "config": config,
                                "runtime": runtime,
                                "baseSystemPrompt": base_system_prompt,
                                "toolRegistry": tool_registry,
                                "activeModel": active_model,
                                "prompt": prompt,
                                "reactiveAttempt": int(prepared_request.get("reactiveAttempt") or 0) + 1,
                            }
                        )
                    except Exception as prepare_error:
                        if _is_turn_abort_error(prepare_error, runtime.get("abortSignal")):
                            return await cancel_turn()
                        raise
                    continue
                if isProviderRequestError(error):
                    session.setdefault("metadata", {})["lastProviderFailure"] = copy.deepcopy(error.record)
                    _safe_emit(runtime, "provider.failed", {"session_id": session["id"], **error.record})
                    await persistProviderFailure(session, config, error.record)
                raise

        if session.setdefault("metadata", {}).get("lastProviderFailure"):
            last = copy.deepcopy(session["metadata"]["lastProviderFailure"])
            recovered_at = _now()
            await persistProviderRecovery(session, config, {"provider": config["provider"], "recoveredAt": recovered_at, "attempts": last.get("attempt", 1), "lastFailure": last})
            session["metadata"].pop("lastProviderFailure", None)

        try:
            _throw_if_turn_cancelled(runtime)
        except Exception as error:
            if _is_turn_abort_error(error, runtime.get("abortSignal")):
                return await cancel_turn()
            raise
        active_model = response["model"]
        session["messages"].append({"role": "assistant", "content": response["content"]})
        merge_usage(total_usage, response.get("usage") or {})
        _merge_usage_into_session(session, response.get("usage") or {})
        recordActualTokenUsage(session, response.get("usage") or {})
        cost += _estimate_usage_cost(response.get("usage") or {}, response["model"])

        text = "".join(block.get("text", "") for block in response["content"] if block.get("type") == "text")
        if text:
            text_parts.append(text)
            _safe_emit(runtime, "message.assistant", {"text": text, "model": response["model"], "session_id": session["id"]})

        tool_uses = [block for block in response["content"] if block.get("type") == "tool_use"]
        if not tool_uses:
            await persistSession(session, config)
            return {"text": "\n\n".join(text_parts).strip(), "toolCount": tool_count, "usage": total_usage, "model": active_model, "estimatedCostUsd": _round_usd(cost), "stopReason": response.get("stopReason")}

        tool_results: list[dict[str, Any] | None] = [None] * len(tool_uses)
        paused_for_approval = False
        paused_message = ""

        def build_context() -> dict[str, Any]:
            return {
                "cwd": session["cwd"],
                "allowedRoots": config.get("allowedRoots", []),
                "config": config,
                "session": session,
                "interactive": runtime.get("interactive", False),
                "outputJson": runtime.get("emitEvent", lambda *_: None),
                "askQuestion": runtime.get("askQuestion", lambda *_: ""),
                "persistSession": lambda: persistSession(session, config),
                "updateCwd": lambda next_cwd: session.__setitem__("cwd", next_cwd),
                "getTools": lambda: runtime.get("tools", []),
                "daemon": runtime.get("daemon"),
                "abortSignal": runtime.get("abortSignal"),
            }

        async def run_single(tool_use: dict[str, Any]) -> dict[str, Any]:
            nonlocal tool_count
            _throw_if_turn_cancelled(runtime)
            if _requires_plan_approval(session) and tool_use["name"] not in PLAN_SAFE_TOOL_NAMES:
                message = f"{tool_use['name']} is not available while plan approval is still required."
                _safe_emit(runtime, "tool.completed", {"name": tool_use["name"], "output": message, "error": True, "session_id": session["id"]})
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": message}
            tool = next((entry for entry in tool_registry if entry.name == tool_use["name"]), None)
            if not tool:
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": f"Tool not available: {tool_use['name']}"}
            tool_count += 1
            usage_totals["toolCalls"] += 1
            _safe_emit(runtime, "tool.started", {"name": tool.name, "input": tool_use.get("input") or {}, "session_id": session["id"]})
            try:
                result = await tool.handler(tool_use.get("input") or {}, build_context())
                _throw_if_turn_cancelled(runtime)
                _safe_emit(runtime, "tool.completed", {"name": tool.name, "output": result.get("content", ""), "error": result.get("isError", False), "session_id": session["id"]})
                block = {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": result.get("isError"), "content": result.get("content", "")}
                if result.get("pause") == "approval":
                    block["_pauseMessage"] = (result.get("content") or "").strip() or "Approval required before continuing."
                return block
            except Exception as exc:
                if _is_turn_abort_error(exc, runtime.get("abortSignal")):
                    raise
                message = str(exc)
                _safe_emit(runtime, "tool.completed", {"name": tool.name, "output": message, "error": True, "session_id": session["id"]})
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": message}

        try:
            if _requires_plan_approval(session):
                for index, tool_use in enumerate(tool_uses):
                    result = await run_single(tool_use)
                    pause = result.pop("_pauseMessage", None)
                    tool_results[index] = result
                    if pause:
                        paused_for_approval = True
                        paused_message = pause
                        for skipped in range(index + 1, len(tool_uses)):
                            tool_results[skipped] = {"type": "tool_result", "tool_use_id": tool_uses[skipped]["id"], "is_error": True, "content": "Skipped: previous tool in the batch paused for approval."}
                        break
            else:
                outcomes = await asyncio.gather(*(run_single(tool_use) for tool_use in tool_uses))
                for index, result in enumerate(outcomes):
                    pause = result.pop("_pauseMessage", None)
                    if pause and not paused_for_approval:
                        paused_for_approval = True
                        paused_message = pause
                    tool_results[index] = result
            _throw_if_turn_cancelled(runtime)
        except Exception as error:
            if _is_turn_abort_error(error, runtime.get("abortSignal")):
                return await cancel_turn()
            raise

        session["messages"].append({"role": "user", "content": [result for result in tool_results if result is not None]})
        await persistSession(session, config)
        if paused_for_approval:
            if paused_message:
                text_parts.append(paused_message)
            return {"text": "\n\n".join(text_parts).strip(), "toolCount": tool_count, "usage": total_usage, "model": active_model, "estimatedCostUsd": _round_usd(cost), "stopReason": "approval_required"}


def extractAssistantText(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content or [] if block.get("type") == "text")


def _create_provider(config: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    if config.get("provider") == "openai":
        if not config.get("openaiToken"):
            return {"send": lambda request: _missing_openai_credential_response(config, request)}
        return {
            "send": lambda request: sendOpenAiRequest(
                token=config["openaiToken"],
                baseUrl=config.get("openaiBaseUrl"),
                headers=config.get("customHeaders"),
                mode=config.get("openaiApiMode"),
                request=request,
                runtime=runtime,
            )
        }
    if not config.get("apiKey") and not config.get("authToken"):
        return {"send": lambda request: _missing_anthropic_credential_response(config, request)}
    return {"send": lambda request: sendAnthropicMessagesRequest(config=config, request=request, runtime=runtime)}


def _missing_openai_credential_response(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    env_var = (((config.get("providerProfile") or {}).get("auth") or {}).get("envVar")) or "the required bearer token env var"
    return {
        "content": [
            {
                "type": "text",
                "text": f"No credential is configured for provider profile '{config.get('providerProfileName')}'. Set {env_var}, provide a Codex auth file, or run `tess auth login --openai` for official OpenAI access.",
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": request["model"],
        "stopReason": "end_turn",
    }


def _missing_anthropic_credential_response(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    env_var = (((config.get("providerProfile") or {}).get("auth") or {}).get("envVar")) or "the required Anthropic credential env var"
    return {
        "content": [
            {
                "type": "text",
                "text": f"No credential is configured for provider profile '{config.get('providerProfileName')}'. Set {env_var} or run `tess auth login` to enable model-backed responses.",
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": request["model"],
        "stopReason": "end_turn",
    }


def _apply_plan_mode_overlay(base: str, session: dict[str, Any]) -> str:
    if session.get("permissionMode") != "plan":
        return base
    state = session.get("metadata", {}).get("planMode", {})
    return buildPlanModeSystemPrompt(base, {"currentState": state.get("state"), "priorPlan": state.get("plan")})


def _requires_plan_approval(session: dict[str, Any]) -> bool:
    return session.get("permissionMode") == "plan" and session.get("metadata", {}).get("planMode", {}).get("state") != "approved"


def _ensure_usage_totals(session: dict[str, Any]) -> dict[str, int]:
    metadata = session.setdefault("metadata", {})
    metadata.setdefault("usage", {"turns": 0, "toolCalls": 0, "inputTokens": 0, "outputTokens": 0, "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0})
    return metadata["usage"]


def _merge_usage_into_session(session: dict[str, Any], usage: dict[str, Any]) -> None:
    totals = _ensure_usage_totals(session)
    totals["inputTokens"] += int(usage.get("input_tokens") or 0)
    totals["outputTokens"] += int(usage.get("output_tokens") or 0)
    totals["cacheCreationInputTokens"] += int(usage.get("cache_creation_input_tokens") or 0)
    totals["cacheReadInputTokens"] += int(usage.get("cache_read_input_tokens") or 0)


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


async def prepareProviderRequest(args: dict[str, Any]) -> dict[str, Any]:
    session = args["session"]
    config = args["config"]
    runtime = args["runtime"]
    base_system_prompt = args["baseSystemPrompt"]
    tool_registry = args["toolRegistry"]
    active_model = args["activeModel"]
    prompt = args["prompt"]
    reactive_attempt = int(args.get("reactiveAttempt") or 0)
    prepared_request: dict[str, Any] | None = None

    for _attempt in range(4):
        request_context = await buildContinuationContext(
            {
                "session": session,
                "config": config,
                "prompt": prompt,
                "aggressive": reactive_attempt > 0,
                "reactiveAttempt": reactive_attempt,
            }
        )
        emitContinuationRelief(runtime, session["id"], request_context)
        recordContinuationRelief(session, request_context)
        system = _apply_plan_mode_overlay(base_system_prompt, session)
        if request_context.get("systemContext"):
            system = f"{system}\n\n{request_context['systemContext']}"
        request = {
            "model": active_model,
            "effort": normalizeEffortForModel(config["provider"], active_model, session.get("effort") or config.get("effort")),
            "system": system,
            "messages": request_context["messages"],
            "tools": [{"name": tool.name, "description": tool.description, "input_schema": tool.inputSchema} for tool in tool_registry],
        }
        prepared_request = {"request": request, "continuity": request_context, "reactiveAttempt": reactive_attempt}
        measurement = await measureRequestInputTokens({"config": config, "request": request, "session": session, "abortSignal": runtime.get("abortSignal")})
        compacted = await maybeAutoCompactSession(
            {
                "session": session,
                "config": config,
                "runtime": runtime,
                "currentInputTokens": measurement["inputTokens"],
                "currentInputTokensSource": measurement.get("source"),
            }
        )
        if compacted:
            continue
        recordPreflightTokenMeasurement(session, measurement)
        _safe_emit(
            runtime,
            "provider.input_tokens",
            {
                "session_id": session["id"],
                "model": request["model"],
                "provider": config["provider"],
                "input_tokens": measurement["inputTokens"],
                "source": measurement["source"],
                "base_input_tokens": measurement.get("baseInputTokens"),
                "safety_margin_tokens": measurement.get("safetyMarginTokens"),
                "continuity_pressure": request_context.get("pressure"),
                "continuity_relief": request_context.get("relief"),
                "retrieved_memory_count": len((request_context.get("retrievedMemory") or {}).get("records", [])),
                "retrieved_memory_tokens": (request_context.get("retrievedMemory") or {}).get("tokenEstimate"),
            },
        )
        await persistSession(session, config)
        return prepared_request

    if prepared_request:
        return prepared_request

    request_context = await buildContinuationContext(
        {
            "session": session,
            "config": config,
            "prompt": prompt,
            "aggressive": reactive_attempt > 0,
            "reactiveAttempt": reactive_attempt,
        }
    )
    emitContinuationRelief(runtime, session["id"], request_context)
    recordContinuationRelief(session, request_context)
    system = _apply_plan_mode_overlay(base_system_prompt, session)
    if request_context.get("systemContext"):
        system = f"{system}\n\n{request_context['systemContext']}"
    return {
        "request": {
            "model": active_model,
            "effort": normalizeEffortForModel(config["provider"], active_model, session.get("effort") or config.get("effort")),
            "system": system,
            "messages": request_context["messages"],
            "tools": [{"name": tool.name, "description": tool.description, "input_schema": tool.inputSchema} for tool in tool_registry],
        },
        "continuity": request_context,
        "reactiveAttempt": reactive_attempt,
    }


async def maybeAutoCompactSession(args: dict[str, Any]) -> dict[str, Any] | None:
    session = args["session"]
    config = args["config"]
    runtime = args["runtime"]
    current_input_tokens = args.get("currentInputTokens")
    current_input_tokens_source = args.get("currentInputTokensSource")
    decision = shouldAutoCompact(session, current_input_tokens)
    if not decision.get("shouldCompact") or not decision.get("trigger"):
        return None

    _safe_emit(
        runtime,
        "compaction.started",
        {
            "session_id": session["id"],
            "trigger": decision["trigger"],
            "current_token_estimate": decision["currentTokenEstimate"],
            "current_token_source": current_input_tokens_source,
        },
    )

    try:
        outcome = await runCompaction({"session": session, "config": config, "trigger": decision["trigger"]})
    except Exception as error:
        provider_failure = error.record if isProviderRequestError(error) else None
        normalized_error = provider_failure.get("message") if provider_failure else str(error)
        _safe_emit(
            runtime,
            "compaction.failed",
            {
                "session_id": session["id"],
                "trigger": decision["trigger"],
                "current_token_estimate": decision["currentTokenEstimate"],
                "current_token_source": current_input_tokens_source,
                "error": normalized_error,
                "provider": (provider_failure or {}).get("provider"),
                "provider_label": (provider_failure or {}).get("providerLabel"),
                "category": (provider_failure or {}).get("category"),
                "phase": (provider_failure or {}).get("phase"),
                "retryable": (provider_failure or {}).get("retryable"),
                "attempt": (provider_failure or {}).get("attempt"),
                "status": (provider_failure or {}).get("status"),
                "code": (provider_failure or {}).get("code"),
                "error_type": (provider_failure or {}).get("errorType"),
                "raw_error": (provider_failure or {}).get("rawMessage"),
            },
        )
        if provider_failure:
            await persistProviderFailure(session, config, provider_failure)
        if decision["trigger"] == "auto_soft_limit":
            return None
        raise

    if not outcome or outcome.get("status") == "skipped":
        _safe_emit(
            runtime,
            "compaction.skipped",
            {
                "session_id": session["id"],
                "trigger": decision["trigger"],
                "current_token_estimate": decision["currentTokenEstimate"],
                "current_token_source": current_input_tokens_source,
                "reason": (outcome or {}).get("reason"),
                "detail": (outcome or {}).get("detail"),
                "previous_cutoff": (outcome or {}).get("previousCutoff"),
                "candidate_cutoff": (outcome or {}).get("candidateCutoff"),
                "quality_score": (outcome or {}).get("qualityScore"),
                "quality_classification": (outcome or {}).get("qualityClassification"),
            },
        )
        return None

    result = outcome
    metadata = session.setdefault("metadata", {})
    metadata["runtimeRestore"] = {
        **(
            metadata.get("runtimeRestore")
            or {
                "savedAt": _now(),
                "activeTasks": [],
                "restoredTaskCount": len(metadata.get("taskIds") or []),
            }
        ),
        "savedAt": _now(),
        "lastCompactionMode": "full_checkpoint_compaction",
        "lastContextReliefMode": "full_checkpoint_compaction",
        "lastContextReliefPressureLevel": "critical",
        "lastContextReliefReactiveAttempt": None,
        "lastContextReliefDetail": "full checkpoint compaction replaced older context with working-context artifacts",
        "lastContextReliefReinjectionSummary": "working context, decisions, todos, and recent raw tail were preserved after full checkpoint compaction",
    }
    if metadata.get("compactionState"):
        metadata["compactionState"]["lastContextReliefMode"] = "full_checkpoint_compaction"
        metadata["compactionState"]["lastReliefModes"] = ["full_checkpoint_compaction"]
        metadata["compactionState"]["lastContextReliefPressureLevel"] = "critical"
        metadata["compactionState"]["lastReliefReactiveAttempt"] = None
        metadata["compactionState"]["lastReliefDetail"] = "full checkpoint compaction replaced older context with working-context artifacts"
        metadata["compactionState"]["lastReinjectionSummary"] = "working context, decisions, todos, and recent raw tail were preserved after full checkpoint compaction"
    _safe_emit(
        runtime,
        "compaction.completed",
        {
            "session_id": session["id"],
            "generation": result.get("generation"),
            "trigger": result.get("trigger"),
            "compacted_through_message_index": result.get("compactedThroughMessageIndex"),
            "compacted_message_count": result.get("compactedMessageCount"),
            "recent_message_count": result.get("recentMessageCount"),
            "raw_token_estimate": result.get("rawTokenEstimate"),
            "working_context_token_estimate": result.get("workingContextTokenEstimate"),
            "working_context_token_source": (result.get("workingContextTokenMeasurement") or {}).get("source") if result.get("workingContextTokenMeasurement") else None,
            "quality_classification": result.get("qualityClassification"),
            "compaction_provider": (result.get("compactionModel") or {}).get("provider"),
            "compaction_model": (result.get("compactionModel") or {}).get("model"),
            "verification_provider": (result.get("verificationModel") or {}).get("provider"),
            "verification_model": (result.get("verificationModel") or {}).get("model"),
            "verification_verdict": (result.get("verification") or {}).get("verdict"),
            "verification_confidence": (result.get("verification") or {}).get("confidence"),
            "summary_path": (result.get("artifacts") or {}).get("summary"),
            "state_path": (result.get("artifacts") or {}).get("state"),
            "working_context_path": (result.get("artifacts") or {}).get("workingContext"),
            "checkpoint_path": (result.get("artifacts") or {}).get("checkpoint"),
        },
    )
    await persistSession(session, config)
    return result


def emitContinuationRelief(runtime: dict[str, Any], session_id: str, continuity: dict[str, Any]) -> None:
    relief = continuity.get("relief") or {}
    pressure = continuity.get("pressure") or {}
    if relief.get("selectedMode") != "none":
        _safe_emit(
            runtime,
            "context_relief.selected",
            {
                "session_id": session_id,
                "mode": relief.get("selectedMode"),
                "reason": relief.get("detail"),
                "token_estimate": pressure.get("totalTokenEstimate"),
                "pressure_level": relief.get("pressureLevel"),
                "reactive_attempt": relief.get("reactiveAttempt"),
                "planned_modes": relief.get("plannedModes"),
            },
        )
    if relief.get("appliedModes"):
        _safe_emit(
            runtime,
            "context_relief.applied",
            {
                "session_id": session_id,
                "mode": relief.get("selectedMode"),
                "modes": relief.get("appliedModes"),
                "tokens_saved_estimate": relief.get("tokensSavedEstimate"),
                "dropped_message_count": relief.get("droppedMessageCount"),
                "compacted_message_count": relief.get("compactedMessageCount"),
                "detail": relief.get("detail"),
                "pressure_level": relief.get("pressureLevel"),
                "reactive_attempt": relief.get("reactiveAttempt"),
                "reinjection_summary": relief.get("reinjectionSummary"),
                "reinjection_section_count": len(relief.get("reinjectionLines") or []),
            },
        )


def recordContinuationRelief(session: dict[str, Any], continuity: dict[str, Any]) -> None:
    relief = continuity.get("relief") or {}
    metadata = session.setdefault("metadata", {})
    metadata["runtimeRestore"] = {
        **(
            metadata.get("runtimeRestore")
            or {
                "savedAt": _now(),
                "activeTasks": [],
                "restoredTaskCount": len(metadata.get("taskIds") or []),
            }
        ),
        "savedAt": _now(),
        "lastContextReliefMode": relief.get("selectedMode"),
        "lastContextReliefPressureLevel": relief.get("pressureLevel"),
        "lastContextReliefReactiveAttempt": relief.get("reactiveAttempt"),
        "lastContextReliefDetail": relief.get("detail"),
        "lastContextReliefReinjectionSummary": relief.get("reinjectionSummary"),
    }
    if metadata.get("compactionState"):
        metadata["compactionState"]["lastContextReliefMode"] = relief.get("selectedMode")
        metadata["compactionState"]["lastReliefModes"] = relief.get("appliedModes")
        metadata["compactionState"]["lastContextReliefPressureLevel"] = relief.get("pressureLevel")
        metadata["compactionState"]["lastReliefReactiveAttempt"] = relief.get("reactiveAttempt")
        metadata["compactionState"]["lastReliefDetail"] = relief.get("detail")
        metadata["compactionState"]["lastReinjectionSummary"] = relief.get("reinjectionSummary")


def isContextWindowFailure(error: BaseException) -> bool:
    if not isProviderRequestError(error):
        return False
    record = getattr(error, "record", {})
    detail = f"{record.get('rawMessage') or ''} {record.get('rawBody') or ''} {error}".lower()
    return bool(re.search(r"context|input token|prompt token|max token|maximum context length|too large|too long|context_length|request exceeds|input is too long|input too long", detail))


def _safe_emit(runtime: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    try:
        runtime.get("emitEvent", lambda *_: None)(event, data)
    except Exception:
        pass


def _is_turn_cancelled(runtime: dict[str, Any]) -> bool:
    return _signal_aborted(runtime.get("abortSignal"))


def _throw_if_turn_cancelled(runtime: dict[str, Any]) -> None:
    signal = runtime.get("abortSignal")
    if not _signal_aborted(signal):
        return
    reason = _signal_reason(signal)
    if isinstance(reason, BaseException):
        raise reason
    error = RuntimeError(str(reason or "Turn cancelled"))
    error.name = "AbortError"  # type: ignore[attr-defined]
    raise error


def _is_turn_abort_error(error: BaseException, signal: Any = None) -> bool:
    if _signal_aborted(signal):
        return True
    if error.__class__.__name__ == "AbortError":
        return True
    return getattr(error, "name", None) == "AbortError"


def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))


def _signal_reason(signal: Any) -> Any:
    if not signal:
        return None
    if isinstance(signal, dict):
        return signal.get("reason")
    return getattr(signal, "reason", None)


def _restore_session_state(session: dict[str, Any], snapshot: dict[str, Any]) -> None:
    preserved_permission_mode = session.get("permissionMode")
    session.clear()
    session.update(copy.deepcopy(snapshot))
    if preserved_permission_mode is not None:
        session["permissionMode"] = preserved_permission_mode


def _now() -> str:
    from ._utils import now_iso

    return now_iso()
