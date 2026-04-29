from __future__ import annotations

from .shared import *

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
