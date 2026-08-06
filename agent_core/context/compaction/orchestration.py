from __future__ import annotations

import math

import os
import uuid
from ..._utils import now_iso
from ...storage import (
    ensureBaseLayout,
    getSessionCompactionsDir,
    loadCompactionManifest,
    saveCompactionCheckpoint,
    saveCompactionManifest,
)
from ..compaction_models import createDefaultCompactionModelClient
from ..durable_memory import updateDurableMemoryFromWorkingContext
from .artifacts import _write_json, _write_text, buildArtifactPaths
from .cutoffs import chooseCompactionCutoff, resolveLatestPlan
from .documents import (
    assertModelBackedCompactionArtifacts,
    buildCompactionQualityFromCandidate,
    buildCompactionQualityFromSynthesis,
    buildModelBackedCompactionStateSnapshot,
    buildSynthesizedWorkingContextDocument,
    buildWorkingContextDocumentFromCandidate,
    loadWorkingContextArtifact,
    renderActivityMarkdown,
    renderDecisionsMarkdown,
    renderPlanMarkdown,
    renderSummaryMarkdown,
)
from .models import (
    _verification_record,
    runCompactionCheckpointVerificationWithFallback,
    runModelBackedCompactionSynthesisWithFallback,
)
from .prompts import renderWorkingContextPromptContext
from .shared import (
    COMPACTION_STRATEGY,
    CONTINUATION_BRIDGE_MESSAGE,
    WORKING_CONTEXT_COMPACTION_TOKEN_LIMIT,
)
from .utils import estimateMessageTokens, estimateTextTokens
from typing import Any
from ..compaction_state import refreshCompactionStateFromManifest as _refresh_compaction_state_from_manifest
from ...payloads.media_persistence import sanitizeMediaForPersistence as _sanitize_media_for_persistence
from ...session.message_origin import syntheticUserMessage as _synthetic_user_message
from ...providers.token_budget import DEFAULT_COMPACTION_PRESERVATION_RATIO as _DEFAULT_COMPACTION_PRESERVATION_RATIO
from ...providers.usage_costs import mergeProviderUsage as _merge_provider_usage


async def runCompaction(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    config = args.get("config") or {}
    runtime = args.get("runtime") or {}
    trigger = args.get("trigger", "manual")
    force = bool(args.get("force", False))
    model_client = args.get("modelClient") or args.get("model_client") or createDefaultCompactionModelClient(
        {"openaiClientFactory": runtime.get("openaiClientFactory") or config.get("openaiClientFactory")}
    )
    await _refresh_compaction_state_from_manifest(session)
    messages = session.get("messages", [])
    previous_cutoff = int(session.get("metadata", {}).get("compactionState", {}).get("compactedThroughMessageIndex") or 0)

    if not messages:
        return {
            "status": "skipped",
            "trigger": trigger,
            "reason": "no_legal_cutoff",
            "detail": "No safe compaction boundary is available yet.",
            "previousCutoff": previous_cutoff,
        }

    cutoff = chooseCompactionCutoff(messages, force, previous_cutoff)
    if cutoff is None or cutoff <= 0:
        return {
            "status": "skipped",
            "trigger": trigger,
            "reason": "no_legal_cutoff",
            "detail": "No safe compaction boundary is available yet.",
            "previousCutoff": previous_cutoff,
        }
    if cutoff <= previous_cutoff:
        return {
            "status": "skipped",
            "trigger": trigger,
            "reason": "already_compacted_no_new_source",
            "detail": "Working context is already compacted through the newest valid boundary.",
            "previousCutoff": previous_cutoff,
            "candidateCutoff": cutoff,
        }

    source_messages = _sanitize_media_for_persistence(
        messages[previous_cutoff:cutoff]
    )
    recent_messages = messages[cutoff:]
    raw_token_estimate = estimateMessageTokens(messages)
    source_token_estimate = estimateMessageTokens(source_messages)
    current_input_tokens = int(args.get("currentInputTokens") or 0)
    compaction_threshold_tokens = int(args.get("compactionThresholdTokens") or 0)
    compaction_target_tokens = int(
        args.get("compactionTargetTokens") or compaction_threshold_tokens
    )
    target_estimated_relief = max(
        0,
        source_token_estimate - math.ceil(source_token_estimate * _DEFAULT_COMPACTION_PRESERVATION_RATIO),
    )
    if (
        trigger in {"auto_hard_limit", "auto_target_headroom"}
        and previous_cutoff > 0
        and current_input_tokens > 0
        and compaction_target_tokens > 0
        and current_input_tokens - target_estimated_relief > compaction_target_tokens
    ):
        return {
            "status": "skipped",
            "trigger": trigger,
            "reason": "insufficient_incremental_relief",
            "detail": (
                "The new compactable transcript slice is too small to bring the measured request below the compaction "
                "headroom target, even at the target preservation ratio. Rewriting the durable checkpoint would not provide "
                "meaningful context relief."
            ),
            "previousCutoff": previous_cutoff,
            "candidateCutoff": cutoff,
            "sourceTokenEstimate": source_token_estimate,
            "targetEstimatedRelief": target_estimated_relief,
            "currentInputTokens": current_input_tokens,
            "compactionThresholdTokens": compaction_threshold_tokens,
            "compactionTargetTokens": compaction_target_tokens,
        }

    await ensureBaseLayout(session.get("cwd") or os.getcwd())
    os.makedirs(os.path.join(getSessionCompactionsDir(session["id"]), "checkpoints"), exist_ok=True)

    previous_manifest = await loadCompactionManifest(session["id"])
    generation = int((previous_manifest or {}).get("latestGeneration") or 0) + 1
    compaction_id = str(uuid.uuid4())
    created_at = now_iso()
    artifacts = buildArtifactPaths(session["id"], generation)

    previous_working_context = await loadWorkingContextArtifact(((previous_manifest or {}).get("artifacts") or {}).get("workingContext"))
    latest_plan = await resolveLatestPlan(session)
    synthesized = await runModelBackedCompactionSynthesisWithFallback(
        {
            "session": session,
            "config": config,
            "runtime": runtime,
            "modelClient": model_client,
            "previousWorkingContext": previous_working_context,
            "latestPlan": latest_plan,
            "sourceMessages": source_messages,
            "sourceStart": previous_cutoff,
        }
    )
    if not synthesized.get("shouldCheckpoint", True):
        return {
            "status": "skipped",
            "trigger": trigger,
            "reason": "quality_gate_failed",
            "detail": synthesized.get("qualityReason"),
            "previousCutoff": previous_cutoff,
            "candidateCutoff": cutoff,
            "qualityClassification": synthesized.get("qualityClassification"),
        }

    quality = buildCompactionQualityFromSynthesis(synthesized)
    source_range = {"generation": generation, "start": previous_cutoff, "end": cutoff, "messageCount": len(source_messages), "createdAt": created_at}
    working_context = buildSynthesizedWorkingContextDocument(
        {
            "previous": previous_working_context,
            "session": session,
            "generation": generation,
            "createdAt": created_at,
            "latestPlan": latest_plan,
            "sourceRange": source_range,
            "synthesized": synthesized,
        }
    )
    verification = await runCompactionCheckpointVerificationWithFallback(
        {
            "session": session,
            "config": config,
            "runtime": runtime,
            "modelClient": model_client,
            "previousWorkingContext": previous_working_context,
            "latestPlan": latest_plan,
            "sourceMessages": source_messages,
            "sourceStart": previous_cutoff,
            "candidate": {
                "qualityClassification": quality["classification"],
                "qualityReason": quality["detail"],
                "workingContext": synthesized["workingContext"],
            },
        }
    )
    verification_usage: dict[str, Any] = {}
    _merge_provider_usage(verification_usage, verification.get("usage") or {})
    repair_count = 0

    if verification.get("verdict") == "repair" and verification.get("repairedCheckpoint"):
        repair_count = 1
        repaired = verification["repairedCheckpoint"]
        quality = buildCompactionQualityFromCandidate(repaired)
        working_context = buildWorkingContextDocumentFromCandidate(
            {
                "previous": previous_working_context,
                "session": session,
                "generation": generation,
                "createdAt": created_at,
                "latestPlan": latest_plan,
                "sourceRange": source_range,
                "candidate": {**repaired, "model": synthesized["model"]},
                "preservedTraceEvidence": verification.get("evidence"),
            }
        )
        repaired_verification = await runCompactionCheckpointVerificationWithFallback(
            {
                "session": session,
                "config": config,
                "runtime": runtime,
                "modelClient": model_client,
                "previousWorkingContext": previous_working_context,
                "latestPlan": latest_plan,
                "sourceMessages": source_messages,
                "sourceStart": previous_cutoff,
                "candidate": repaired,
                "repairAttempt": 1,
            }
        )
        _merge_provider_usage(verification_usage, repaired_verification.get("usage") or {})
        verification = {**repaired_verification, "usage": verification_usage}

    if verification.get("verdict") in {"reject", "repair"}:
        return {
            "status": "skipped",
            "trigger": trigger,
            "reason": "verification_rejected",
            "detail": verification.get("reason"),
            "previousCutoff": previous_cutoff,
            "candidateCutoff": cutoff,
            "qualityClassification": quality["classification"],
        }

    if quality["classification"] == "thin":
        return {
            "status": "skipped",
            "trigger": trigger,
            "reason": "quality_gate_failed",
            "detail": "Final checkpoint quality remained thin after verification; refusing to persist it.",
            "previousCutoff": previous_cutoff,
            "candidateCutoff": cutoff,
            "qualityClassification": quality["classification"],
        }

    state_snapshot = buildModelBackedCompactionStateSnapshot(
        {
            "session": session,
            "generation": generation,
            "createdAt": created_at,
            "sourceStart": previous_cutoff,
            "cutoff": cutoff,
            "sourceMessages": source_messages,
            "previousWorkingContext": previous_working_context,
            "latestPlan": latest_plan,
            "synthesized": synthesized,
            "verification": verification,
            "repairCount": repair_count,
            "quality": quality,
            "workingContext": working_context,
        }
    )
    assertModelBackedCompactionArtifacts(state_snapshot, working_context)

    summary = renderSummaryMarkdown(
        working_context,
        {
            "start": previous_cutoff,
            "end": cutoff,
            "messageCount": len(source_messages),
            "quality": quality,
            "verification": {
                "model": verification["model"],
                "verdict": verification["verdict"],
                "confidence": verification["confidence"],
                "reason": verification["reason"],
                "issues": verification["issues"],
                "notes": verification["notes"],
                "repairCount": repair_count,
            },
        },
    )
    todos = {"sessionId": session["id"], "generatedAt": created_at, "todos": session.get("todos", [])}
    plan_markdown = renderPlanMarkdown(working_context)
    prompt_context = renderWorkingContextPromptContext(working_context)
    compacted_context_token_estimate = estimateTextTokens(prompt_context)
    working_context_token_estimate = compacted_context_token_estimate + estimateMessageTokens(recent_messages)

    _write_text(artifacts["summary"], summary.rstrip() + "\n")
    _write_text(artifacts["decisions"], renderDecisionsMarkdown(working_context).rstrip() + "\n")
    _write_text(artifacts["activity"], renderActivityMarkdown(working_context).rstrip() + "\n")
    _write_json(artifacts["todos"], todos)
    _write_json(artifacts["state"], state_snapshot)
    _write_json(artifacts["workingContext"], working_context)
    if plan_markdown:
        _write_text(artifacts["plan"], plan_markdown.rstrip() + "\n")

    await updateDurableMemoryFromWorkingContext({"session": session, "workingContext": working_context})

    verification_record = _verification_record(verification)
    manifest = {
        "sessionId": session["id"],
        "strategy": COMPACTION_STRATEGY,
        "createdAt": (previous_manifest or {}).get("createdAt") or created_at,
        "updatedAt": created_at,
        "latestGeneration": generation,
        "latestCompactionId": compaction_id,
        "compactedThroughMessageIndex": cutoff,
        "compactedMessageCount": cutoff,
        "recentMessageCount": len(recent_messages),
        "archiveMessageCount": len(messages),
        "rawTokenEstimate": raw_token_estimate,
        "compactedContextTokenEstimate": compacted_context_token_estimate,
        "workingContextTokenEstimate": working_context_token_estimate,
        "workingContextTokenMeasurement": None,
        "lastTrigger": trigger,
        "qualityClassification": quality["classification"],
        "compactionModel": synthesized["model"],
        "verificationModel": verification["model"],
        "verification": verification_record,
        "artifacts": artifacts,
    }
    checkpoint = {
        "id": compaction_id,
        "generation": generation,
        "sessionId": session["id"],
        "createdAt": created_at,
        "trigger": trigger,
        "sourceRange": {"start": previous_cutoff, "end": cutoff, "messageCount": len(source_messages)},
        "rawTokenEstimate": raw_token_estimate,
        "compactedContextTokenEstimate": compacted_context_token_estimate,
        "workingContextTokenEstimate": working_context_token_estimate,
        "workingContextTokenMeasurement": None,
        "qualityClassification": quality["classification"],
        "compactionModel": synthesized["model"],
        "verificationModel": verification["model"],
        "verification": verification_record,
        "artifacts": artifacts,
        "state": state_snapshot,
    }

    await saveCompactionManifest(manifest)
    await saveCompactionCheckpoint(checkpoint)

    metadata = session.setdefault("metadata", {})
    metadata["compactions"] = int(metadata.get("compactions") or 0) + 1
    metadata["compactionState"] = {
        "latestCompactionId": compaction_id,
        "latestCompactionGeneration": generation,
        "latestCompactionAt": created_at,
        "compactedThroughMessageIndex": cutoff,
        "compactedMessageCount": cutoff,
        "recentMessageCount": len(recent_messages),
        "archiveMessageCount": len(messages),
        "rawTokenEstimate": raw_token_estimate,
        "compactedContextTokenEstimate": compacted_context_token_estimate,
        "workingContextTokenEstimate": working_context_token_estimate,
        "workingContextTokenMeasurement": None,
        "lastTrigger": trigger,
        "qualityClassification": quality["classification"],
        "compactionModel": synthesized["model"],
        "verificationModel": verification["model"],
        "verification": verification_record,
        "artifacts": artifacts,
    }

    return {
        "status": "completed",
        "id": compaction_id,
        "generation": generation,
        "trigger": trigger,
        "compactedThroughMessageIndex": cutoff,
        "compactedMessageCount": len(source_messages),
        "recentMessageCount": len(recent_messages),
        "rawTokenEstimate": raw_token_estimate,
        "compactedContextTokenEstimate": compacted_context_token_estimate,
        "workingContextTokenEstimate": working_context_token_estimate,
        "workingContextTokenMeasurement": None,
        "qualityClassification": quality["classification"],
        "compactionModel": synthesized["model"],
        "verificationModel": verification["model"],
        "verification": verification_record,
        "artifacts": artifacts,
    }
