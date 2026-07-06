from __future__ import annotations

from .shared import *


async def maybeAutoCheckpointRawSession(args: dict[str, Any]) -> dict[str, Any] | None:
    session = args["session"]
    config = args["config"]
    runtime = args["runtime"]
    compaction_policy = resolveCompactionPolicy(config.get("providerProfile") or {})
    decision = decideDurableCheckpoint(session, tokenLimit=compaction_policy.get("compactionThresholdTokens"))
    raw_context = decision.get("rawContext") or {}
    _emit_raw_context_pressure(runtime, session["id"], raw_context)
    _safe_emit(runtime, "compaction.decision", _decision_event_payload(session["id"], decision, raw_context))
    if not decision.get("shouldCheckpoint") or not decision.get("trigger"):
        return None
    return await _run_auto_compaction(
        {
            "session": session,
            "config": config,
            "runtime": runtime,
            "trigger": decision["trigger"],
            "currentTokenEstimate": raw_context.get("rawTokenEstimate"),
            "currentTokenSource": "raw_session_estimate",
            "decision": decision,
        }
    )


async def maybeAutoCompactSession(args: dict[str, Any]) -> dict[str, Any] | None:
    session = args["session"]
    compaction_policy = resolveCompactionPolicy(args["config"].get("providerProfile") or {})
    decision = shouldAutoCompact(session, args.get("currentInputTokens"), compaction_policy.get("compactionThresholdTokens"))
    if not decision.get("shouldCompact") or not decision.get("trigger"):
        return None
    return await _run_auto_compaction(
        {
            "session": session,
            "config": args["config"],
            "runtime": args["runtime"],
            "trigger": decision["trigger"],
            "currentTokenEstimate": decision["currentTokenEstimate"],
            "currentTokenSource": args.get("currentInputTokensSource"),
        }
    )


async def _run_auto_compaction(args: dict[str, Any]) -> dict[str, Any] | None:
    session = args["session"]
    config = args["config"]
    runtime = args["runtime"]
    trigger = args["trigger"]
    current_token_estimate = _safe_int(args.get("currentTokenEstimate"))
    current_token_source = args.get("currentTokenSource")
    decision = args.get("decision") or {}
    _safe_emit(
        runtime,
        "compaction.started",
        {
            "session_id": session["id"],
            "trigger": trigger,
            "current_token_estimate": current_token_estimate,
            "current_token_source": current_token_source,
            "decision_reason": decision.get("reason"),
            "raw_token_estimate": (decision.get("rawContext") or {}).get("rawTokenEstimate"),
            "messages_since_last_checkpoint": (decision.get("rawContext") or {}).get("messagesSinceLastCheckpoint"),
            "tool_results_since_last_checkpoint": (decision.get("rawContext") or {}).get("toolResultsSinceLastCheckpoint"),
        },
    )
    try:
        outcome = await runCompaction({"session": session, "config": config, "runtime": runtime, "trigger": trigger})
    except Exception as error:
        await _handle_auto_compaction_failure(
            session=session,
            config=config,
            runtime=runtime,
            trigger=trigger,
            current_token_estimate=current_token_estimate,
            current_token_source=current_token_source,
            decision=decision,
            error=error,
        )
        if not _is_hard_compaction_trigger(trigger):
            return None
        raise

    if not outcome or outcome.get("status") == "skipped":
        _emit_auto_compaction_skip(runtime, session["id"], trigger, current_token_estimate, current_token_source, decision, outcome)
        return None

    result = outcome
    _record_successful_auto_compaction(session)
    _safe_emit(runtime, "compaction.completed", _completion_event_payload(session["id"], result))
    await persistSession(session, config)
    return result


def _decision_event_payload(session_id: str, decision: dict[str, Any], raw_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "should_checkpoint": bool(decision.get("shouldCheckpoint")),
        "trigger": decision.get("trigger"),
        "reason": decision.get("reason"),
        "raw_token_estimate": raw_context.get("rawTokenEstimate"),
        "message_count": raw_context.get("messageCount"),
        "messages_since_last_checkpoint": raw_context.get("messagesSinceLastCheckpoint"),
        "tool_results_since_last_checkpoint": raw_context.get("toolResultsSinceLastCheckpoint"),
        "raw_token_delta_since_last_checkpoint": raw_context.get("rawTokenDeltaSinceLastCheckpoint"),
        "tool_result_token_estimate_since_last_checkpoint": raw_context.get("toolResultTokenEstimateSinceLastCheckpoint"),
        "largest_tool_result_token_estimate": raw_context.get("largestToolResultTokenEstimate"),
        "has_durable_checkpoint": raw_context.get("hasDurableCheckpoint"),
        "latest_compaction_at": raw_context.get("latestCompactionAt"),
    }


async def _handle_auto_compaction_failure(
    *,
    session: dict[str, Any],
    config: dict[str, Any],
    runtime: dict[str, Any],
    trigger: str,
    current_token_estimate: int,
    current_token_source: Any,
    decision: dict[str, Any],
    error: BaseException,
) -> None:
    provider_failure = error.record if isProviderRequestError(error) else None
    _safe_emit(
        runtime,
        "compaction.failed",
        {
            "session_id": session["id"],
            "trigger": trigger,
            "current_token_estimate": current_token_estimate,
            "current_token_source": current_token_source,
            "decision_reason": decision.get("reason"),
            "error": provider_failure.get("message") if provider_failure else str(error),
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


def _emit_auto_compaction_skip(
    runtime: dict[str, Any],
    session_id: str,
    trigger: str,
    current_token_estimate: int,
    current_token_source: Any,
    decision: dict[str, Any],
    outcome: dict[str, Any] | None,
) -> None:
    _safe_emit(
        runtime,
        "compaction.skipped",
        {
            "session_id": session_id,
            "trigger": trigger,
            "current_token_estimate": current_token_estimate,
            "current_token_source": current_token_source,
            "decision_reason": decision.get("reason"),
            "reason": (outcome or {}).get("reason"),
            "detail": (outcome or {}).get("detail"),
            "previous_cutoff": (outcome or {}).get("previousCutoff"),
            "candidate_cutoff": (outcome or {}).get("candidateCutoff"),
            "quality_score": (outcome or {}).get("qualityScore"),
            "quality_classification": (outcome or {}).get("qualityClassification"),
        },
    )


def _record_successful_auto_compaction(session: dict[str, Any]) -> None:
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
        "lastContextReliefKind": "full_checkpoint_compaction",
        "recentContextReliefCount": 0,
        "contextReliefEscalationRequested": False,
        "contextReliefEscalationReason": "",
    }
    if metadata.get("compactionState"):
        metadata["compactionState"]["lastContextReliefMode"] = "full_checkpoint_compaction"
        metadata["compactionState"]["lastReliefModes"] = ["full_checkpoint_compaction"]
        metadata["compactionState"]["lastContextReliefPressureLevel"] = "critical"
        metadata["compactionState"]["lastReliefReactiveAttempt"] = None
        metadata["compactionState"]["lastReliefDetail"] = "full checkpoint compaction replaced older context with working-context artifacts"
        metadata["compactionState"]["lastReinjectionSummary"] = "working context, decisions, todos, and recent raw tail were preserved after full checkpoint compaction"


def _completion_event_payload(session_id: str, result: dict[str, Any]) -> dict[str, Any]:
    measurement = result.get("workingContextTokenMeasurement")
    return {
        "session_id": session_id,
        "generation": result.get("generation"),
        "trigger": result.get("trigger"),
        "compacted_through_message_index": result.get("compactedThroughMessageIndex"),
        "compacted_message_count": result.get("compactedMessageCount"),
        "recent_message_count": result.get("recentMessageCount"),
        "raw_token_estimate": result.get("rawTokenEstimate"),
        "working_context_token_estimate": result.get("workingContextTokenEstimate"),
        "working_context_token_source": (measurement or {}).get("source") if measurement else None,
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
    }


def _emit_raw_context_pressure(runtime: dict[str, Any], session_id: str, pressure: dict[str, Any]) -> None:
    _safe_emit(
        runtime,
        "context.raw_pressure",
        {
            "session_id": session_id,
            "raw_token_estimate": pressure.get("rawTokenEstimate"),
            "message_count": pressure.get("messageCount"),
            "assistant_tool_use_count": pressure.get("assistantToolUseCount"),
            "tool_result_count": pressure.get("toolResultCount"),
            "messages_since_last_checkpoint": pressure.get("messagesSinceLastCheckpoint"),
            "tool_results_since_last_checkpoint": pressure.get("toolResultsSinceLastCheckpoint"),
            "raw_token_delta_since_last_checkpoint": pressure.get("rawTokenDeltaSinceLastCheckpoint"),
            "tool_result_token_estimate_since_last_checkpoint": pressure.get("toolResultTokenEstimateSinceLastCheckpoint"),
            "largest_tool_result_token_estimate": pressure.get("largestToolResultTokenEstimate"),
            "elapsed_ms_since_last_checkpoint": pressure.get("elapsedMsSinceLastCheckpoint"),
            "has_durable_checkpoint": pressure.get("hasDurableCheckpoint"),
            "latest_compaction_at": pressure.get("latestCompactionAt"),
        },
    )


def _is_hard_compaction_trigger(trigger: str) -> bool:
    return trigger in {"auto_hard_limit", "auto_raw_hard_limit"}


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0
