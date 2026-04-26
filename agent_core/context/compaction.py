from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from .._utils import now_iso
from .compaction_models import createDefaultCompactionModelClient, resolveCompactionModelCandidates
from .durable_memory import updateDurableMemoryFromWorkingContext
from ..provider_errors import fallbackEligibleProviderFailure, isProviderRequestError
from ..storage import (
    compactionCheckpointPath,
    compactionManifestPath,
    ensureBaseLayout,
    getSessionCompactionsDir,
    listPlanApprovals,
    loadCompactionManifest,
    saveCompactionCheckpoint,
    saveCompactionManifest,
)


COMPACTION_STRATEGY = "checkpoint-v2"
MIN_RECENT_MESSAGE_COUNT = 20
MIN_COMPACTABLE_MESSAGE_COUNT = 28
RAW_TAIL_TOKEN_TARGET = 100_000
RAW_TAIL_TOKEN_BUDGET = 40_000
SOFT_WORKING_CONTEXT_TOKEN_LIMIT = 150_000
HARD_WORKING_CONTEXT_TOKEN_LIMIT = 180_000
AUTO_SOFT_RETRIGGER_MESSAGE_DELTA = 16
AUTO_SOFT_RETRIGGER_TOKEN_DELTA = 64_000
MAX_PRESERVED_TOOL_EXCHANGE_PAIRS = 10
MIN_PRESERVED_TOOL_EXCHANGE_PAIRS = 3
MAX_ACTIVITY_LINES = 16
MAX_FAILURES = 8
MAX_TODOS = 12
CONTINUATION_BRIDGE_MESSAGE = (
    "Continuation context: earlier history and older parts of the current tool-driven turn were compacted into the working "
    "context provided in the system prompt. Continue from the recent raw messages below."
)
LOW_VALUE_TAIL_TOOL_NAMES = {"SetStatus", "TodoWrite", "Config"}
COMPACTION_SYNTHESIS_MAX_RETRIES = 3
COMPACTION_VERIFICATION_MAX_RETRIES = 3
COMPACTION_EVIDENCE_TOKEN_BUDGET = 90_000
COMPACTION_EVIDENCE_FALLBACK_BUDGETS = [COMPACTION_EVIDENCE_TOKEN_BUDGET, 60_000, 40_000]
COMPACTION_MESSAGE_TEXT_CHAR_LIMIT = 2_400
COMPACTION_TOOL_INPUT_CHAR_LIMIT = 1_200
COMPACTION_TOOL_RESULT_CHAR_LIMIT = 2_000
COMPACTION_COMPACT_TOOL_RESULT_CHAR_LIMIT = 600
COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT = 1_200
COMPACTION_FILE_PREVIEW_LINES = 8
COMPACTION_MATCH_PREVIEW_LIMIT = 8

COMPACTION_SYSTEM_PROMPT = """You synthesize durable working context for a coding agent.

Your job is to replace old transcript history with a compact checkpoint that still lets the agent continue work accurately.

Output rules:
- Return a single JSON object only. No markdown. No prose outside JSON.
- Reconcile the previous working context with the new transcript slice.
- Keep only information that still matters for continuation.
- Remove stale asks, completed one-off requests, chit-chat, routine status chatter, and transient dead-end tool failures unless they still affect the current task.
- Preserve the current user goal, completed implementation work, remaining work, key decisions, relevant files, durable discoveries, active blockers, and any critical repo or runtime facts.
- Prefer exact file paths, exact command names, and exact task state when available.
- Lists should be concise, deduplicated, current, and ordered by usefulness for continuation.
- If the compacted slice is too thin to justify a checkpoint, set shouldCheckpoint to false and explain why.

JSON shape:
{
  "shouldCheckpoint": true,
  "quality": {
    "classification": "thin|usable|rich",
    "reason": "short explanation"
  },
  "workingContext": {
    "project": {
      "description": "string or null",
      "facts": {
        "primaryLanguage": "string or null",
        "languages": ["..."],
        "ecosystem": "string or null",
        "packageManager": "string or null",
        "evidence": ["..."]
      },
      "keyFiles": [
        { "path": "repo-relative-or-absolute-path", "role": "read|modified", "note": "optional short note" }
      ]
    },
    "goals": {
      "active": ["..."],
      "recentRequests": ["..."],
      "constraints": ["..."],
      "preferences": ["..."]
    },
    "currentState": {
      "completed": ["..."],
      "inProgress": ["..."],
      "blocked": ["..."]
    },
    "decisions": ["..."],
    "discoveries": ["..."],
    "activity": {
      "commands": ["..."],
      "searches": ["..."],
      "web": ["..."],
      "failures": ["..."]
    },
    "nextSteps": ["..."],
    "criticalContext": ["..."]
  }
}"""

COMPACTION_VERIFICATION_SYSTEM_PROMPT = """You verify and, when necessary, repair compaction checkpoints for a coding agent.

Your job is to determine whether a candidate checkpoint preserves enough accurate, current information for the agent to continue work correctly after transcript history is compacted.

Output rules:
- Return a single JSON object only. No markdown. No prose outside JSON.
- Use the provided transcript evidence and previous working context as ground truth.
- Prefer rejecting or repairing a checkpoint over accepting a misleading one.
- Remove stale requests, stale blockers, stale commit/push chatter, dead-end failures, and outdated next steps.
- Preserve the active user goal, real completed work, current in-progress work, current blockers, relevant files, durable discoveries, and usable next steps.
- If repair is needed, return a fully repaired checkpoint in repairedCheckpoint.

JSON shape:
{
  "verdict": "accept|accept_with_notes|repair|reject",
  "confidence": "low|medium|high",
  "reason": "short explanation",
  "issues": ["..."],
  "notes": ["..."],
  "repairedCheckpoint": {
    "quality": {
      "classification": "thin|usable|rich",
      "reason": "short explanation"
    },
    "workingContext": {
      "project": {
        "description": "string or null",
        "facts": {
          "primaryLanguage": "string or null",
          "languages": ["..."],
          "ecosystem": "string or null",
          "packageManager": "string or null",
          "evidence": ["..."]
        },
        "keyFiles": [
          { "path": "repo-relative-or-absolute-path", "role": "read|modified", "note": "optional short note" }
        ]
      },
      "goals": {
        "active": ["..."],
        "recentRequests": ["..."],
        "constraints": ["..."],
        "preferences": ["..."]
      },
      "currentState": {
        "completed": ["..."],
        "inProgress": ["..."],
        "blocked": ["..."]
      },
      "decisions": ["..."],
      "discoveries": ["..."],
      "activity": {
        "commands": ["..."],
        "searches": ["..."],
        "web": ["..."],
        "failures": ["..."]
      },
      "nextSteps": ["..."],
      "criticalContext": ["..."]
    }
  }
}"""


def estimateWorkingContextTokens(session: dict[str, Any]) -> int:
    state = session.get("metadata", {}).get("compactionState")
    if not state:
        return estimateMessageTokens(session.get("messages", []))
    cutoff = int(state.get("compactedThroughMessageIndex") or 0)
    recent_messages = session.get("messages", [])[cutoff:]
    return int(state.get("compactedContextTokenEstimate") or 0) + estimateMessageTokens(recent_messages)


def shouldAutoCompact(session: dict[str, Any], currentInputTokens: int | None = None) -> dict[str, Any]:
    current_token_estimate = currentInputTokens if currentInputTokens is not None else estimateWorkingContextTokens(session)
    previous_state = session.get("metadata", {}).get("compactionState")
    if current_token_estimate >= HARD_WORKING_CONTEXT_TOKEN_LIMIT:
        return {"shouldCompact": True, "trigger": "auto_hard_limit", "currentTokenEstimate": current_token_estimate}
    if current_token_estimate >= SOFT_WORKING_CONTEXT_TOKEN_LIMIT:
        if previous_state:
            message_delta = max(0, len(session.get("messages", [])) - int(previous_state.get("archiveMessageCount") or 0))
            measurement = previous_state.get("workingContextTokenMeasurement") or {}
            previous_tokens = int(measurement.get("inputTokens") or previous_state.get("workingContextTokenEstimate") or 0)
            token_delta = max(0, current_token_estimate - previous_tokens)
            if message_delta < AUTO_SOFT_RETRIGGER_MESSAGE_DELTA and token_delta < AUTO_SOFT_RETRIGGER_TOKEN_DELTA:
                return {"shouldCompact": False, "currentTokenEstimate": current_token_estimate}
        return {"shouldCompact": True, "trigger": "auto_soft_limit", "currentTokenEstimate": current_token_estimate}
    return {"shouldCompact": False, "currentTokenEstimate": current_token_estimate}


def buildMessagesForWorkingContext(session: dict[str, Any]) -> list[dict[str, Any]]:
    state = session.get("metadata", {}).get("compactionState")
    if not state:
        return list(session.get("messages", []))
    cutoff = int(state.get("compactedThroughMessageIndex") or 0)
    recent_messages = list(session.get("messages", [])[cutoff:])
    if not recent_messages or recent_messages[0].get("role") == "user":
        return recent_messages
    return [{"role": "user", "content": CONTINUATION_BRIDGE_MESSAGE}, *recent_messages]


async def buildCompactionSystemPromptContext(session: dict[str, Any]) -> str | None:
    state = session.get("metadata", {}).get("compactionState")
    if not state:
        return None
    artifacts = state.get("artifacts") or {}
    working_context = await loadWorkingContextArtifact(artifacts.get("workingContext"))
    if working_context:
        return renderWorkingContextPromptContext(working_context)
    summary_path = artifacts.get("summary")
    summary = _read_text(summary_path).strip() if summary_path else ""
    if not summary:
        return None
    return "\n".join(
        [
            "Archived working context from earlier turns and compacted portions of the current turn is available below.",
            "Use it as authoritative background for earlier work and earlier compacted activity in this turn.",
            "When it conflicts with the recent raw turns, prefer the recent raw turns.",
            "",
            summary,
        ]
    )


async def runCompaction(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    config = args.get("config") or {}
    trigger = args.get("trigger", "manual")
    force = bool(args.get("force", False))
    model_client = args.get("modelClient") or args.get("model_client") or createDefaultCompactionModelClient()
    messages = session.get("messages", [])
    previous_cutoff = int(session.get("metadata", {}).get("compactionState", {}).get("compactedThroughMessageIndex") or 0)

    if len(messages) < (MIN_RECENT_MESSAGE_COUNT + 2 if force else MIN_COMPACTABLE_MESSAGE_COUNT):
        return {
            "status": "skipped",
            "trigger": trigger,
            "reason": "below_threshold",
            "detail": "Conversation does not yet have enough history to produce a useful checkpoint.",
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
            "reason": "already_up_to_date",
            "detail": "Working context is already compacted through the newest valid boundary.",
            "previousCutoff": previous_cutoff,
            "candidateCutoff": cutoff,
        }

    source_messages = messages[previous_cutoff:cutoff]
    recent_messages = messages[cutoff:]
    raw_token_estimate = estimateMessageTokens(messages)

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
            }
        )
        verification = await runCompactionCheckpointVerificationWithFallback(
            {
                "session": session,
                "config": config,
                "modelClient": model_client,
                "previousWorkingContext": previous_working_context,
                "latestPlan": latest_plan,
                "sourceMessages": source_messages,
                "sourceStart": previous_cutoff,
                "candidate": repaired,
                "repairAttempt": 1,
            }
        )

    mergeCompactionUsageIntoSession(session, synthesized.get("usage") or {})
    mergeCompactionUsageIntoSession(session, verification.get("usage") or {})

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


def buildCompactionAttemptStrategies(modelCandidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for evidence_budget in COMPACTION_EVIDENCE_FALLBACK_BUDGETS:
        for model in modelCandidates:
            key = ":".join([str(model.get("purpose")), str(model.get("provider")), str(model.get("model")), str(model.get("effort") or ""), str(evidence_budget)])
            if key in seen:
                continue
            seen.add(key)
            attempts.append({"model": model, "evidenceTokenBudget": evidence_budget})
    return attempts


async def runModelBackedCompactionSynthesisWithFallback(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    candidates = resolveCompactionModelCandidates({"config": args.get("config") or {}, "session": args["session"], "purpose": "synthesis"})
    attempts = buildCompactionAttemptStrategies(candidates)
    last_error: Exception | None = None
    for index, attempt in enumerate(attempts):
        try:
            return await runModelBackedCompactionSynthesis({**args, "model": attempt["model"], "evidenceTokenBudget": attempt["evidenceTokenBudget"]})
        except Exception as exc:
            last_error = exc
            if not shouldRetryCompactionAttempt(exc, index < len(attempts) - 1):
                raise
    raise last_error or RuntimeError("Compaction synthesis failed.")


async def runModelBackedCompactionSynthesis(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    model = args["model"]
    evidence_budget = int(args.get("evidenceTokenBudget") or COMPACTION_EVIDENCE_TOKEN_BUDGET)
    evidence = buildCompactionEvidence(
        {
            "sourceMessages": args["sourceMessages"],
            "sourceStart": int(args.get("sourceStart") or 0),
            "cwd": session.get("cwd") or os.getcwd(),
            "tokenBudget": evidence_budget,
        }
    )
    last_error = "No compaction synthesis response was returned."
    for attempt in range(1, COMPACTION_SYNTHESIS_MAX_RETRIES + 1):
        response = await _call_model_client(
            args["modelClient"],
            {
                "config": args.get("config") or {},
                "model": model,
                "system": COMPACTION_SYSTEM_PROMPT,
                "prompt": buildCompactionSynthesisPrompt(
                    {
                        "session": session,
                        "model": model,
                        "previousWorkingContext": args.get("previousWorkingContext"),
                        "latestPlan": args.get("latestPlan"),
                        "sourceStart": int(args.get("sourceStart") or 0),
                        "sourceEnd": int(args.get("sourceStart") or 0) + len(args["sourceMessages"]),
                        "evidence": evidence,
                        "previousError": last_error if attempt > 1 else None,
                    }
                ),
            },
        )
        raw_response_text = str(response.get("text") or "").strip()
        parsed = parseCompactionSynthesisResponse(raw_response_text)
        if parsed:
            return {**parsed, "usage": response.get("usage") or {}, "rawResponseText": raw_response_text, "evidence": evidence, "evidenceBudgetTokens": evidence_budget, "model": response.get("model") or model}
        last_error = f"Attempt {attempt} returned invalid compaction JSON: {truncate(raw_response_text, 1200)}"
    raise RuntimeError(f"Compaction synthesis failed: {last_error}")


async def runCompactionCheckpointVerificationWithFallback(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    candidates = resolveCompactionModelCandidates({"config": args.get("config") or {}, "session": args["session"], "purpose": "verification"})
    attempts = buildCompactionAttemptStrategies(candidates)
    last_error: Exception | None = None
    for index, attempt in enumerate(attempts):
        try:
            return await runCompactionCheckpointVerification({**args, "model": attempt["model"], "evidenceTokenBudget": attempt["evidenceTokenBudget"]})
        except Exception as exc:
            last_error = exc
            if not shouldRetryCompactionAttempt(exc, index < len(attempts) - 1):
                raise
    raise last_error or RuntimeError("Compaction verification failed.")


async def runCompactionCheckpointVerification(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    model = args["model"]
    evidence_budget = int(args.get("evidenceTokenBudget") or COMPACTION_EVIDENCE_TOKEN_BUDGET)
    source_start = int(args.get("sourceStart") or 0)
    evidence = buildCompactionEvidence(
        {
            "sourceMessages": args["sourceMessages"],
            "sourceStart": source_start,
            "cwd": session.get("cwd") or os.getcwd(),
            "tokenBudget": evidence_budget,
        }
    )
    last_error = "No compaction verification response was returned."
    for attempt in range(1, COMPACTION_VERIFICATION_MAX_RETRIES + 1):
        response = await _call_model_client(
            args["modelClient"],
            {
                "config": args.get("config") or {},
                "model": model,
                "system": COMPACTION_VERIFICATION_SYSTEM_PROMPT,
                "prompt": buildCompactionVerificationPrompt(
                    {
                        "session": session,
                        "model": model,
                        "previousWorkingContext": args.get("previousWorkingContext"),
                        "latestPlan": args.get("latestPlan"),
                        "sourceStart": source_start,
                        "sourceEnd": source_start + len(args["sourceMessages"]),
                        "evidence": evidence,
                        "candidate": args["candidate"],
                        "repairAttempt": int(args.get("repairAttempt") or 0),
                        "previousError": last_error if attempt > 1 else None,
                    }
                ),
            },
        )
        raw_response_text = str(response.get("text") or "").strip()
        parsed = parseCompactionVerificationResponse(raw_response_text)
        if parsed:
            return {**parsed, "usage": response.get("usage") or {}, "rawResponseText": raw_response_text, "evidence": evidence, "evidenceBudgetTokens": evidence_budget, "model": response.get("model") or model}
        last_error = f"Attempt {attempt} returned invalid compaction verification JSON: {truncate(raw_response_text, 1200)}"
    raise RuntimeError(f"Compaction verification failed: {last_error}")


def buildCompactionSynthesisPrompt(args: dict[str, Any]) -> str:
    session = args["session"]
    evidence = args["evidence"]
    model = args["model"]
    todo_snapshot = {
        "open": [summarizeTodo(todo) for todo in session.get("todos", []) if todo.get("status") != "completed"],
        "completed": [summarizeTodo(todo) for todo in session.get("todos", []) if todo.get("status") == "completed"],
    }
    lines = [
        "Synthesize the next working context for this session.",
        "",
        "Session metadata:",
        json.dumps(
            {
                "sessionId": session.get("id"),
                "cwd": session.get("cwd"),
                "liveProvider": session.get("provider"),
                "liveModel": session.get("model"),
                "liveEffort": session.get("effort"),
                "permissionMode": session.get("permissionMode"),
                "compactionProvider": model.get("provider"),
                "compactionModel": model.get("model"),
                "compactionEffort": model.get("effort"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        "",
        f"Compacted source range: messages {args['sourceStart']}-{max(args['sourceEnd'] - 1, args['sourceStart'])}",
        f"Evidence entries: {evidence['entryCount']}",
        f"Evidence included: {evidence['includedEntryCount']}",
        f"Evidence omitted: {evidence['omittedEntryCount']}",
        f"Evidence token budget: {evidence['tokenBudget']}",
        f"Evidence token estimate: {evidence['tokenEstimate']}",
        f"Evidence truncated: {str(evidence['truncated']).lower()}",
        "",
        "Exact current todo snapshot:",
        json.dumps(todo_snapshot, indent=2, ensure_ascii=False),
        "",
        "Latest saved plan state:",
        json.dumps(args.get("latestPlan"), indent=2, ensure_ascii=False),
        "",
        "Previous working context:",
        json.dumps(args.get("previousWorkingContext"), indent=2, ensure_ascii=False),
        "",
        "Compacted transcript evidence:",
        evidence["transcript"],
        "",
        "Important guidance:",
        "- The recent raw transcript after the cutoff remains available to the live agent separately. Focus this checkpoint on compacted history and durable continuation context.",
        "- Keep only still-relevant blockers. Do not carry forward stale grep failures, formatting misses, or completed commit/push requests unless they remain actionable.",
        "- Prefer current objectives over superseded earlier asks.",
        "- Treat the todo snapshot and latest saved plan state as authoritative.",
        "- Return JSON only.",
    ]
    if args.get("previousError"):
        lines.extend(["", "Previous output error:", args["previousError"]])
    return "\n".join(lines)


def buildCompactionVerificationPrompt(args: dict[str, Any]) -> str:
    session = args["session"]
    evidence = args["evidence"]
    model = args["model"]
    candidate = args["candidate"]
    lines = [
        "Verify this candidate compaction checkpoint against the transcript evidence.",
        "",
        "Session metadata:",
        json.dumps(
            {
                "sessionId": session.get("id"),
                "cwd": session.get("cwd"),
                "liveProvider": session.get("provider"),
                "liveModel": session.get("model"),
                "liveEffort": session.get("effort"),
                "verificationProvider": model.get("provider"),
                "verificationModel": model.get("model"),
                "verificationEffort": model.get("effort"),
                "repairAttempt": args.get("repairAttempt", 0),
            },
            indent=2,
            ensure_ascii=False,
        ),
        "",
        f"Compacted source range: messages {args['sourceStart']}-{max(args['sourceEnd'] - 1, args['sourceStart'])}",
        f"Evidence entries: {evidence['entryCount']}",
        f"Evidence included: {evidence['includedEntryCount']}",
        f"Evidence omitted: {evidence['omittedEntryCount']}",
        f"Evidence token budget: {evidence['tokenBudget']}",
        f"Evidence token estimate: {evidence['tokenEstimate']}",
        f"Evidence truncated: {str(evidence['truncated']).lower()}",
        "",
        "Latest saved plan state:",
        json.dumps(args.get("latestPlan"), indent=2, ensure_ascii=False),
        "",
        "Previous working context:",
        json.dumps(args.get("previousWorkingContext"), indent=2, ensure_ascii=False),
        "",
        "Candidate checkpoint:",
        json.dumps(
            {
                "quality": {"classification": candidate.get("qualityClassification"), "reason": candidate.get("qualityReason")},
                "workingContext": candidate.get("workingContext"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        "",
        "Compacted transcript evidence:",
        evidence["transcript"],
        "",
        "Important guidance:",
        "- Reject or repair checkpoints that carry stale goals, stale blockers, stale commit/push asks, or incorrect next steps.",
        "- Prefer the current active user goal and durable implementation state.",
        "- If you repair, return a fully repaired checkpoint in repairedCheckpoint.",
        "- Return JSON only.",
    ]
    if args.get("previousError"):
        lines.extend(["", "Previous output error:", args["previousError"]])
    return "\n".join(lines)


def parseCompactionSynthesisResponse(value: str) -> dict[str, Any] | None:
    candidate = extractJsonObjectCandidate(value)
    if not candidate:
        return None
    parsed = parseMaybeJson(candidate)
    record = asRecord(parsed)
    if not record:
        return None
    quality = asRecord(record.get("quality")) or {}
    classification = quality.get("classification") if quality.get("classification") in {"thin", "usable", "rich"} else "usable"
    reason = collapseWhitespace(quality.get("reason")) if isinstance(quality.get("reason"), str) else "Model-backed checkpoint synthesis completed."
    working_context = coerceSynthesizedWorkingContextFields(record.get("workingContext"))
    if not working_context:
        return None
    return {
        "shouldCheckpoint": record.get("shouldCheckpoint") is not False,
        "qualityClassification": classification,
        "qualityReason": reason,
        "workingContext": working_context,
    }


def parseCompactionVerificationResponse(value: str) -> dict[str, Any] | None:
    candidate = extractJsonObjectCandidate(value)
    if not candidate:
        return None
    parsed = parseMaybeJson(candidate)
    record = asRecord(parsed)
    if not record:
        return None
    verdict = normalizeVerificationVerdict(record.get("verdict"))
    if not verdict:
        return None
    confidence = normalizeVerificationConfidence(record.get("confidence"))
    reason = collapseWhitespace(record.get("reason")) if isinstance(record.get("reason"), str) else "Checkpoint verification completed."
    issues = asStringArray(record.get("issues"))[:12]
    notes = asStringArray(record.get("notes"))[:12]
    repaired = coerceRepairedCheckpoint(record.get("repairedCheckpoint")) if verdict == "repair" else None
    if verdict == "repair" and not repaired:
        return None
    return {"verdict": verdict, "confidence": confidence, "reason": reason, "issues": issues, "notes": notes, "repairedCheckpoint": repaired}


def buildCompactionQualityFromSynthesis(synthesized: dict[str, Any]) -> dict[str, Any]:
    return buildCompactionQualityFromCandidate(
        {
            "qualityClassification": synthesized.get("qualityClassification"),
            "qualityReason": synthesized.get("qualityReason"),
            "workingContext": synthesized.get("workingContext"),
        }
    )


def buildCompactionQualityFromCandidate(candidate: dict[str, Any]) -> dict[str, Any]:
    classification = candidate.get("qualityClassification") if candidate.get("qualityClassification") in {"thin", "usable", "rich"} else "usable"
    score = 16 if classification == "rich" else 8 if classification == "usable" else 3
    working_context = candidate.get("workingContext") or {}
    goals = asRecord(working_context.get("goals")) or {}
    project = asRecord(working_context.get("project")) or {}
    activity = asRecord(working_context.get("activity")) or {}
    return {
        "passed": classification != "thin",
        "score": score,
        "classification": classification,
        "detail": candidate.get("qualityReason") or "Model-backed checkpoint synthesis completed.",
        "metrics": {
            "userRequests": len(asStringArray(goals.get("active"))),
            "relevantFiles": len(coerceRelevantFiles(project.get("keyFiles"))),
            "decisions": len(asStringArray(working_context.get("decisions"))),
            "discoveries": len(asStringArray(working_context.get("discoveries"))),
            "reads": 0,
            "fileChanges": 0,
            "commands": len(asStringArray(activity.get("commands"))),
            "searches": len(asStringArray(activity.get("searches"))),
            "web": len(asStringArray(activity.get("web"))),
            "failures": len(asStringArray(activity.get("failures"))),
        },
    }


def buildSynthesizedWorkingContextDocument(args: dict[str, Any]) -> dict[str, Any]:
    return buildWorkingContextDocumentFromCandidate(
        {
            "previous": args.get("previous"),
            "session": args["session"],
            "generation": args["generation"],
            "createdAt": args["createdAt"],
            "latestPlan": args.get("latestPlan"),
            "sourceRange": args["sourceRange"],
            "candidate": {
                "qualityClassification": args["synthesized"]["qualityClassification"],
                "qualityReason": args["synthesized"]["qualityReason"],
                "workingContext": args["synthesized"]["workingContext"],
                "model": args["synthesized"]["model"],
            },
        }
    )


def buildWorkingContextDocumentFromCandidate(args: dict[str, Any]) -> dict[str, Any]:
    previous = args.get("previous") or {}
    session = args["session"]
    candidate = args["candidate"]
    working_context = candidate["workingContext"]
    model = candidate["model"]
    open_todos = [summarizeTodo(todo) for todo in session.get("todos", []) if todo.get("status") != "completed"][:MAX_TODOS]
    completed_todos = [summarizeTodo(todo) for todo in session.get("todos", []) if todo.get("status") == "completed"][-MAX_TODOS:]
    return {
        "version": "v2",
        "sessionId": session["id"],
        "generatedAt": previous.get("generatedAt") or args["createdAt"],
        "updatedAt": args["createdAt"],
        "generation": args["generation"],
        "compaction": {
            "provider": model.get("provider"),
            "model": model.get("model"),
            "effort": model.get("effort"),
            "selectionSource": model.get("selectionSource"),
        },
        "sourceRanges": [*(previous.get("sourceRanges") or []), args["sourceRange"]][-12:],
        "session": {
            "cwd": session.get("cwd"),
            "provider": session.get("provider"),
            "model": session.get("model"),
            "effort": session.get("effort"),
            "permissionMode": session.get("permissionMode"),
        },
        "project": working_context["project"],
        "goals": working_context["goals"],
        "currentState": working_context["currentState"],
        "decisions": working_context["decisions"],
        "discoveries": working_context["discoveries"],
        "plan": args.get("latestPlan") or previous.get("plan"),
        "todos": {"open": open_todos, "completed": completed_todos},
        "activity": working_context["activity"],
        "nextSteps": working_context["nextSteps"],
        "criticalContext": working_context["criticalContext"],
    }


def buildModelBackedCompactionStateSnapshot(args: dict[str, Any]) -> dict[str, Any]:
    synthesized = args["synthesized"]
    verification = args["verification"]
    quality = args["quality"]
    return {
        "sessionId": args["session"]["id"],
        "generatedAt": args["createdAt"],
        "generation": args["generation"],
        "sourceRange": {"start": args["sourceStart"], "end": args["cutoff"], "messageCount": len(args["sourceMessages"])},
        "compactionModel": _model_record(synthesized["model"]),
        "verificationModel": _model_record(verification["model"]),
        "previousWorkingContextGeneration": (args.get("previousWorkingContext") or {}).get("generation"),
        "latestPlan": args.get("latestPlan"),
        "synthesis": {
            "shouldCheckpoint": synthesized.get("shouldCheckpoint"),
            "qualityClassification": synthesized.get("qualityClassification"),
            "qualityReason": synthesized.get("qualityReason"),
            "evidence": synthesized.get("evidence"),
            "rawResponsePreview": truncate(synthesized.get("rawResponseText") or "", 4000),
            "usage": synthesized.get("usage") or {},
        },
        "verification": {
            "verdict": verification.get("verdict"),
            "confidence": verification.get("confidence"),
            "reason": verification.get("reason"),
            "issues": verification.get("issues") or [],
            "notes": verification.get("notes") or [],
            "repairCount": args.get("repairCount") or 0,
            "rawResponsePreview": truncate(verification.get("rawResponseText") or "", 4000),
            "usage": verification.get("usage") or {},
        },
        "evidenceBudget": {
            "tokenLimit": synthesized.get("evidenceBudgetTokens"),
            "verificationTokenLimit": verification.get("evidenceBudgetTokens"),
            "includedEntries": synthesized.get("evidence", {}).get("includedEntryCount"),
            "omittedEntries": synthesized.get("evidence", {}).get("omittedEntryCount"),
            "tokenEstimate": synthesized.get("evidence", {}).get("tokenEstimate"),
            "truncated": synthesized.get("evidence", {}).get("truncated"),
        },
        "assessment": {
            "passed": quality["passed"],
            "score": quality["score"],
            "classification": quality["classification"],
            "detail": quality["detail"],
        },
        "workingContext": args["workingContext"],
    }


def buildCompactionEvidence(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    source_messages = args.get("sourceMessages") or []
    source_start = int(args.get("sourceStart") or 0)
    cwd = args.get("cwd") or os.getcwd()
    token_budget = int(args.get("tokenBudget") or COMPACTION_EVIDENCE_TOKEN_BUDGET)
    tool_uses: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, str]] = []
    for offset, message in enumerate(source_messages):
        entries.extend(buildEvidenceEntriesForMessage(message, source_start + offset, tool_uses, cwd))
    rendered = [entry["full"] for entry in entries]
    transcript = "\n\n".join(rendered)
    token_estimate = estimateTextTokens(transcript)
    included_entry_count = len(entries)
    omitted_entry_count = 0
    truncated = False

    for index, entry in enumerate(entries):
        if token_estimate <= token_budget:
            break
        if rendered[index] == entry["compact"]:
            continue
        rendered[index] = entry["compact"]
        transcript = "\n\n".join(rendered)
        token_estimate = estimateTextTokens(transcript)
        truncated = True

    while rendered and token_estimate > token_budget:
        rendered.pop(0)
        omitted_entry_count += 1
        included_entry_count -= 1
        transcript = "\n\n".join(rendered)
        token_estimate = estimateTextTokens(transcript)
        truncated = True

    if omitted_entry_count:
        transcript = "\n\n".join(
            [
                f"NOTE: {omitted_entry_count} older evidence entries were omitted to keep the compaction synthesis request within budget.",
                "\n\n".join(rendered),
            ]
        ).strip()
        token_estimate = estimateTextTokens(transcript)

    return {
        "transcript": transcript,
        "entryCount": len(entries),
        "includedEntryCount": included_entry_count,
        "omittedEntryCount": omitted_entry_count,
        "tokenEstimate": token_estimate,
        "tokenBudget": token_budget,
        "truncated": truncated,
    }


def buildEvidenceEntriesForMessage(message: dict[str, Any], messageIndex: int, toolUses: dict[str, dict[str, Any]], cwd: str) -> list[dict[str, str]]:
    content = message.get("content")
    role = message.get("role", "user")
    if isinstance(content, str):
        return [buildTextEvidenceEntry({"messageIndex": messageIndex, "role": role, "label": "user_message" if role == "user" else "assistant_text", "text": content})]
    if not isinstance(content, list):
        return []

    entries: list[dict[str, str]] = []
    text_blocks = "\n\n".join(str(block.get("text", "")) for block in content if block.get("type") == "text").strip()
    if text_blocks:
        entries.append(buildTextEvidenceEntry({"messageIndex": messageIndex, "role": role, "label": "user_text_block" if role == "user" else "assistant_text_block", "text": text_blocks}))
    for block in content:
        if block.get("type") == "tool_use":
            tool_uses_id = str(block.get("id") or "")
            toolUses[tool_uses_id] = {"id": tool_uses_id, "name": block.get("name") or "UnknownTool", "input": block.get("input") or {}, "messageIndex": messageIndex}
            entries.append(buildToolUseEvidenceEntry(messageIndex, str(block.get("name") or "UnknownTool"), asRecord(block.get("input")) or {}))
        elif block.get("type") == "tool_result":
            entries.append(buildToolResultEvidenceEntry(messageIndex, block, toolUses.get(str(block.get("tool_use_id") or "")), cwd))
    return entries


def buildTextEvidenceEntry(args: dict[str, Any]) -> dict[str, str]:
    normalized = collapseWhitespace(str(args.get("text") or ""))
    full_body = truncate(normalized, COMPACTION_MESSAGE_TEXT_CHAR_LIMIT)
    compact_body = truncate(normalized, 480)
    return {
        "full": f"[{args['messageIndex']}] {args['role']} {args['label']}\n{full_body}",
        "compact": f"[{args['messageIndex']}] {args['role']} {args['label']}\n{compact_body}",
    }


def buildToolUseEvidenceEntry(messageIndex: int, toolName: str, input: dict[str, Any]) -> dict[str, str]:
    serialized_input = truncate(json.dumps(input, indent=2, ensure_ascii=False), COMPACTION_TOOL_INPUT_CHAR_LIMIT)
    compact_input = truncate(json.dumps(input, ensure_ascii=False), 320)
    return {
        "full": f"[{messageIndex}] assistant tool_use {toolName}\ninput={serialized_input}",
        "compact": f"[{messageIndex}] assistant tool_use {toolName}\ninput={compact_input}",
    }


def buildToolResultEvidenceEntry(messageIndex: int, block: dict[str, Any], toolUse: dict[str, Any] | None, cwd: str) -> dict[str, str]:
    tool_name = (toolUse or {}).get("name") or "UnknownTool"
    content = str(block.get("content") or "")
    full = summarizeToolResultForCompaction(content, cwd, COMPACTION_TOOL_RESULT_CHAR_LIMIT)
    compact = summarizeToolResultForCompaction(content, cwd, COMPACTION_COMPACT_TOOL_RESULT_CHAR_LIMIT)
    prefix = f"[{messageIndex}] user tool_result {tool_name}{' error' if block.get('is_error') else ''}"
    return {"full": f"{prefix}\n{full}", "compact": f"{prefix}\n{compact}"}


def summarizeToolResultForCompaction(content: str, cwd: str, limit: int) -> str:
    parsed = parseMaybeJson(content)
    record = asRecord(parsed)
    if not record:
        return truncate(collapseWhitespace(content), limit)
    file_record = asRecord(record.get("file"))
    if file_record and isinstance(file_record.get("filePath"), str) and isinstance(file_record.get("content"), str):
        file_path = trimPathEvidence(file_record["filePath"], cwd)
        preview = buildFilePreview(file_record["content"])
        parts = [
            f"file={file_path}",
            f"start_line={file_record['startLine']}" if isinstance(file_record.get("startLine"), (int, float)) and file_record.get("startLine") else None,
            f"total_lines={file_record['totalLines']}" if isinstance(file_record.get("totalLines"), (int, float)) and file_record.get("totalLines") else None,
            f"num_lines={file_record['numLines']}" if isinstance(file_record.get("numLines"), (int, float)) and file_record.get("numLines") else None,
            f"preview={preview}" if preview else None,
        ]
        return truncate("\n".join(part for part in parts if part), limit)

    if isinstance(record.get("command"), str) or "stdout" in record or "stderr" in record:
        parts = [
            f"command={collapseWhitespace(record['command'])}" if isinstance(record.get("command"), str) else None,
            f"cwd={record['cwd']}" if isinstance(record.get("cwd"), str) else None,
            f"exit_code={record['exitCode']}" if isinstance(record.get("exitCode"), (int, float)) and not isinstance(record.get("exitCode"), bool) else None,
            f"timed_out={record['timedOut']}" if isinstance(record.get("timedOut"), bool) else None,
            f"stdout={truncate(collapseWhitespace(record['stdout']), COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT)}" if isinstance(record.get("stdout"), str) else None,
            f"stderr={truncate(collapseWhitespace(record['stderr']), COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT)}" if isinstance(record.get("stderr"), str) else None,
        ]
        return truncate("\n".join(part for part in parts if part), limit)

    if isinstance(record.get("matches"), list):
        matches = [trimPathEvidence(value, cwd) for value in record["matches"] if isinstance(value, str)][:COMPACTION_MATCH_PREVIEW_LIMIT]
        parts = [
            f"type={record['type']}" if isinstance(record.get("type"), str) else None,
            f"pattern={record['pattern']}" if isinstance(record.get("pattern"), str) else None,
            f"query={record['query']}" if isinstance(record.get("query"), str) else None,
            f"path={trimPathEvidence(record['path'], cwd)}" if isinstance(record.get("path"), str) else None,
            f"match_count={len(record['matches'])}",
            f"matches={', '.join(matches)}" if matches else None,
        ]
        return truncate("\n".join(part for part in parts if part), limit)
    return truncate(json.dumps(compactStructuredRecord(record, cwd), indent=2, ensure_ascii=False), limit)


def compactStructuredRecord(record: dict[str, Any], cwd: str) -> dict[str, Any]:
    next_record: dict[str, Any] = {}
    keys = ["type", "path", "pattern", "query", "cwd", "command", "exitCode", "timedOut", "message", "detail", "note", "url", "title", "status", "startLine", "totalLines", "numLines"]
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            next_record[key] = trimPathEvidence(value, cwd) if "path" in key.lower() else truncate(value, 400)
        elif isinstance(value, (int, float, bool)):
            next_record[key] = value
    if isinstance(record.get("matches"), list):
        next_record["matches"] = record["matches"][:COMPACTION_MATCH_PREVIEW_LIMIT]
        next_record["matchCount"] = len(record["matches"])
    if isinstance(record.get("content"), str):
        next_record["contentPreview"] = truncate(collapseWhitespace(record["content"]), 600)
    return next_record or {"preview": truncate(json.dumps(record, ensure_ascii=False), 600)}


def buildFilePreview(content: str) -> str:
    lines = content.split("\n")
    if len(lines) <= COMPACTION_FILE_PREVIEW_LINES * 2:
        return truncate(collapseWhitespace(content), COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT)
    head = " ".join(lines[:COMPACTION_FILE_PREVIEW_LINES]).strip()
    tail = " ".join(lines[-COMPACTION_FILE_PREVIEW_LINES:]).strip()
    return truncate(f"{collapseWhitespace(head)} ... {collapseWhitespace(tail)}", COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT)


def extractJsonObjectCandidate(value: str) -> str | None:
    trimmed = value.strip()
    unfenced = re.sub(r"^```json\s*", "", trimmed, flags=re.IGNORECASE)
    unfenced = re.sub(r"^```\s*", "", unfenced, flags=re.IGNORECASE)
    unfenced = re.sub(r"\s*```$", "", unfenced, flags=re.IGNORECASE).strip()
    if unfenced.startswith("{") and unfenced.endswith("}"):
        return unfenced
    start = unfenced.find("{")
    end = unfenced.rfind("}")
    if start >= 0 and end > start:
        return unfenced[start : end + 1]
    return None


def mergeCompactionUsageIntoSession(session: dict[str, Any], usage: dict[str, Any]) -> None:
    metadata = session.setdefault("metadata", {})
    current = metadata.setdefault(
        "usage",
        {"turns": 0, "toolCalls": 0, "inputTokens": 0, "outputTokens": 0, "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0},
    )
    current["inputTokens"] = int(current.get("inputTokens") or 0) + int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
    current["outputTokens"] = int(current.get("outputTokens") or 0) + int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
    current["cacheCreationInputTokens"] = int(current.get("cacheCreationInputTokens") or 0) + int(usage.get("cache_creation_input_tokens") or usage.get("cacheCreationInputTokens") or 0)
    current["cacheReadInputTokens"] = int(current.get("cacheReadInputTokens") or 0) + int(usage.get("cache_read_input_tokens") or usage.get("cacheReadInputTokens") or 0)


def buildArtifactPaths(sessionId: str, generation: int) -> dict[str, str]:
    directory = getSessionCompactionsDir(sessionId)
    return {
        "manifest": compactionManifestPath(sessionId),
        "summary": os.path.join(directory, "summary.md"),
        "state": os.path.join(directory, "state.json"),
        "workingContext": os.path.join(directory, "working-context.json"),
        "decisions": os.path.join(directory, "decisions.md"),
        "todos": os.path.join(directory, "todos.json"),
        "plan": os.path.join(directory, "plan.md"),
        "activity": os.path.join(directory, "activity.md"),
        "checkpoint": compactionCheckpointPath(sessionId, generation),
    }


def chooseCompactionCutoff(messages: list[dict[str, Any]], force: bool = False, previousCutoff: int = 0) -> int | None:
    if len(messages) < (MIN_RECENT_MESSAGE_COUNT + 2 if force else MIN_COMPACTABLE_MESSAGE_COUNT):
        return None
    tool_aware_cutoff = chooseToolAwareCompactionCutoff(messages, previousCutoff)
    if tool_aware_cutoff is not None and tool_aware_cutoff > previousCutoff:
        return tool_aware_cutoff

    fallback_index = max(1, len(messages) - MIN_RECENT_MESSAGE_COUNT)
    index = len(messages)
    token_count = 0
    message_count = 0
    while index > 0 and (message_count < MIN_RECENT_MESSAGE_COUNT or token_count < RAW_TAIL_TOKEN_TARGET):
        index -= 1
        token_count += estimateMessageTokens([messages[index]])
        message_count += 1
    if index == 0:
        index = fallback_index

    for cursor in range(index, 0, -1):
        if isSafeWorkingSetStart(messages, cursor):
            return cursor
    for cursor in range(fallback_index, 0, -1):
        if isSafeWorkingSetStart(messages, cursor):
            return cursor
    return None


def chooseToolAwareCompactionCutoff(messages: list[dict[str, Any]], previousCutoff: int = 0) -> int | None:
    pair_starts = collectSubstantiveToolExchangeStarts(messages, previousCutoff)
    if not pair_starts:
        return None
    preserved_starts = pair_starts[-min(MAX_PRESERVED_TOOL_EXCHANGE_PAIRS, len(pair_starts)) :]
    while len(preserved_starts) > MIN_PRESERVED_TOOL_EXCHANGE_PAIRS:
        cutoff = preserved_starts[0]
        if estimateMessageTokens(messages[cutoff:]) <= RAW_TAIL_TOKEN_BUDGET:
            return cutoff
        preserved_starts = preserved_starts[1:]
    return preserved_starts[0]


def collectSubstantiveToolExchangeStarts(messages: list[dict[str, Any]], previousCutoff: int = 0) -> list[int]:
    tool_uses: dict[str, dict[str, Any]] = {}
    starts: set[int] = set()
    for message_index, message in enumerate(messages):
        if message_index < previousCutoff or not isinstance(message.get("content"), list):
            continue
        if message.get("role") == "assistant":
            for block in message["content"]:
                if block.get("type") == "tool_use":
                    tool_uses[str(block.get("id"))] = {"name": block.get("name"), "messageIndex": message_index}
            continue
        for block in message["content"]:
            if block.get("type") != "tool_result":
                continue
            tool_use = tool_uses.get(str(block.get("tool_use_id")))
            if not tool_use or tool_use.get("name") in LOW_VALUE_TAIL_TOOL_NAMES or int(tool_use.get("messageIndex") or 0) <= previousCutoff:
                continue
            starts.add(int(tool_use["messageIndex"]))
    return sorted(starts)


def isSafeWorkingSetStart(messages: list[dict[str, Any]], index: int) -> bool:
    current = messages[index] if 0 <= index < len(messages) else None
    if not current or current.get("role") != "user":
        return False
    if messageHasToolResults(current):
        return False
    previous = messages[index - 1] if index > 0 else None
    if previous and previous.get("role") == "assistant" and messageHasToolUses(previous):
        return False
    return True


def messageHasToolUses(message: dict[str, Any]) -> bool:
    return isinstance(message.get("content"), list) and any(block.get("type") == "tool_use" for block in message["content"])


def messageHasToolResults(message: dict[str, Any]) -> bool:
    return isinstance(message.get("content"), list) and any(block.get("type") == "tool_result" for block in message["content"])


async def resolveLatestPlan(session: dict[str, Any]) -> dict[str, Any] | None:
    approvals = [record for record in await listPlanApprovals() if record.get("sessionId") == session.get("id") or record.get("parentSessionId") == session.get("id")]
    approvals = sorted(approvals, key=lambda record: record.get("createdAt", ""), reverse=True)
    latest_approval = approvals[0] if approvals else None
    current_plan_state = session.get("metadata", {}).get("planMode")
    file_path = None
    if isinstance(current_plan_state, dict):
        file_path = current_plan_state.get("filePath")
    if not file_path and latest_approval:
        file_path = latest_approval.get("filePath")
    content = _read_text(file_path) if file_path else ""
    if not current_plan_state and not latest_approval and not file_path:
        return None
    request_id = (current_plan_state or {}).get("requestId") if isinstance(current_plan_state, dict) and "requestId" in current_plan_state else (latest_approval or {}).get("requestId")
    status = (current_plan_state or {}).get("state") if isinstance(current_plan_state, dict) and current_plan_state.get("state") is not None else (latest_approval or {}).get("status")
    return {
        "requestId": request_id,
        "status": status,
        "filePath": file_path,
        "exists": bool(file_path and content),
        "content": content or None,
    }


def renderSummaryMarkdown(context: dict[str, Any], meta: dict[str, Any]) -> str:
    session = context.get("session") or {}
    compaction = context.get("compaction") or {}
    lines = [
        "# Assistant Working Checkpoint",
        "",
        "Archive preserved. This checkpoint captures the compacted history that has been removed from the live model context.",
        "",
        "## Session",
        "",
        f"- session: `{context.get('sessionId')}`",
        f"- generation: `{context.get('generation')}`",
        f"- cwd: `{session.get('cwd')}`",
        f"- provider: `{session.get('provider')}`",
        f"- model: `{session.get('model')}`",
        f"- permission mode: `{session.get('permissionMode')}`",
    ]
    if compaction:
        model_label = f"{compaction.get('provider')}:{compaction.get('model')}"
        suffix = f" (`{compaction.get('effort')}`)" if compaction.get("effort") else ""
        via = f" via `{compaction.get('selectionSource')}`" if compaction.get("selectionSource") else ""
        lines.append(f"- compaction model: `{model_label}`{suffix}{via}")
    verification = meta["verification"]
    quality = meta["quality"]
    lines.extend(["", "## Checkpoint Scope", ""])
    lines.append(f"- compacted range: messages {meta['start']}-{max(meta['end'] - 1, meta['start'])} ({meta['messageCount']} messages)")
    lines.append(f"- checkpoint quality: {quality['classification']} (score {quality['score']})")
    verification_model = verification["model"]
    suffix = f" (`{verification_model.get('effort')}`)" if verification_model.get("effort") else ""
    lines.append(f"- verification: `{verification['verdict']}` by `{verification_model.get('provider')}:{verification_model.get('model')}`{suffix} at `{verification['confidence']}` confidence")
    if verification.get("repairCount", 0) > 0:
        lines.append(f"- verifier repairs applied: {verification['repairCount']}")
    if quality["classification"] != "rich":
        lines.append(f"- note: {quality['detail']}")
    if verification.get("reason"):
        lines.append(f"- verification note: {verification['reason']}")
    appendProjectFactsSection(lines, context)
    appendStringSection(lines, "User Goal", context["goals"]["active"])
    appendStringSection(lines, "Constraints And Preferences", [*context["goals"]["constraints"], *context["goals"]["preferences"]])
    appendStringSection(lines, "Discoveries", context["discoveries"])
    appendRelevantFilesSection(lines, context["project"].get("keyFiles") or [])
    lines.extend(["", "## Current State", ""])
    appendSubsection(lines, "Completed", context["currentState"]["completed"])
    appendSubsection(lines, "In Progress", context["currentState"]["inProgress"])
    appendSubsection(lines, "Blocked", context["currentState"]["blocked"])
    appendStringSection(lines, "Key Decisions", context["decisions"])
    appendStringSection(lines, "Verification Notes", [*verification.get("notes", []), *[f"Issue: {entry}" for entry in verification.get("issues", [])]])
    appendPlanSection(lines, context)
    activity = context["activity"]
    appendStringSection(
        lines,
        "Activity Highlights",
        [
            *[f"Search: {entry}" for entry in activity.get("searches", [])[:6]],
            *[f"Command: {entry}" for entry in activity.get("commands", [])[:4]],
            *[f"Web: {entry}" for entry in activity.get("web", [])[:4]],
            *[f"Failure: {entry}" for entry in activity.get("failures", [])[:4]],
        ],
    )
    todos = context.get("todos") or {}
    appendStringSection(lines, "Todo State", [*[f"Open: {entry}" for entry in todos.get("open", [])], *[f"Completed: {entry}" for entry in todos.get("completed", [])]])
    appendStringSection(lines, "Next Steps", context["nextSteps"])
    appendStringSection(lines, "Critical Context", context["criticalContext"])
    lines.extend(["", "## Resume Guidance", "", "- Use this checkpoint as the authoritative source for earlier compacted work.", "- Prefer the recent raw conversation turns when they conflict with this checkpoint.", ""])
    return "\n".join(lines)


def renderDecisionsMarkdown(context: dict[str, Any]) -> str:
    lines = ["# Assistant Decisions", ""]
    plan = context.get("plan") or {}
    if plan.get("filePath"):
        lines.append(f"- Use `{plan['filePath']}` as the latest saved plan reference.")
    if plan.get("status"):
        lines.append(f"- Current plan status is `{plan['status']}`.")
    for decision in context.get("decisions", [])[:12]:
        lines.append(f"- {decision}")
    if len(lines) == 2:
        lines.append("- No durable decisions extracted yet.")
    lines.append("")
    return "\n".join(lines)


def renderActivityMarkdown(context: dict[str, Any]) -> str:
    lines = ["# Assistant Activity Ledger", ""]
    relevant_files = []
    for record in (context.get("project", {}).get("keyFiles") or [])[:MAX_ACTIVITY_LINES]:
        note = f" ({record.get('note')})" if record.get("note") else ""
        role = "modified" if record.get("role") == "modified" else "read"
        relevant_files.append(f"- {role} `{record.get('path')}`{note}")
    appendActivitySection(
        lines,
        "Relevant Files",
        relevant_files,
    )
    activity = context.get("activity") or {}
    appendActivitySection(lines, "Commands", [f"- {entry}" for entry in activity.get("commands", [])[:MAX_ACTIVITY_LINES]])
    appendActivitySection(lines, "Searches", [f"- {entry}" for entry in activity.get("searches", [])[:MAX_ACTIVITY_LINES]])
    appendActivitySection(lines, "Web", [f"- {entry}" for entry in activity.get("web", [])[:MAX_ACTIVITY_LINES]])
    appendActivitySection(lines, "Failures", [f"- {entry}" for entry in activity.get("failures", [])[:MAX_FAILURES]])
    return "\n".join(lines) + "\n"


def renderPlanMarkdown(context: dict[str, Any]) -> str | None:
    content = (context.get("plan") or {}).get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None


def renderWorkingContextPromptContext(context: dict[str, Any]) -> str:
    session = context.get("session") or {}
    compaction = context.get("compaction") or {}
    project = context.get("project") or {}
    facts = project.get("facts") or {}
    lines = [
        "Archived working context from earlier turns and compacted portions of the current turn is available below.",
        "Use it as authoritative background for earlier work and earlier compacted activity in this turn.",
        "When it conflicts with the recent raw turns, prefer the recent raw turns.",
        "",
        "<working_context>",
        f"session={context.get('sessionId')}",
        f"generation={context.get('generation')}",
        f"cwd={session.get('cwd')}",
        f"provider={session.get('provider')}",
        f"model={session.get('model')}",
        f"permission_mode={session.get('permissionMode')}",
    ]
    if compaction:
        lines.append(f"compaction_model={compaction.get('provider')}:{compaction.get('model')}")
        if compaction.get("effort"):
            lines.append(f"compaction_effort={compaction.get('effort')}")
    if project.get("description"):
        lines.append(f"project_description={project['description']}")
    if facts.get("primaryLanguage"):
        lines.append(f"primary_language={facts['primaryLanguage']}")
    if facts.get("ecosystem"):
        lines.append(f"ecosystem={facts['ecosystem']}")
    if facts.get("packageManager"):
        lines.append(f"package_manager={facts['packageManager']}")
    goals = context.get("goals") or {}
    current_state = context.get("currentState") or {}
    activity = context.get("activity") or {}
    todos = context.get("todos") or {}
    appendPromptList(lines, "active_goals", goals.get("active") or [])
    appendPromptList(lines, "recent_requests", goals.get("recentRequests") or [])
    appendPromptList(lines, "constraints", goals.get("constraints") or [])
    appendPromptList(lines, "preferences", goals.get("preferences") or [])
    appendPromptList(lines, "completed", current_state.get("completed") or [])
    appendPromptList(lines, "in_progress", current_state.get("inProgress") or [])
    appendPromptList(lines, "blocked", current_state.get("blocked") or [])
    appendPromptList(lines, "decisions", context.get("decisions") or [])
    appendPromptList(lines, "discoveries", context.get("discoveries") or [])
    appendPromptList(lines, "relevant_files", [_format_relevant_file_prompt(record) for record in project.get("keyFiles", [])])
    appendPromptList(lines, "project_evidence", facts.get("evidence") or [])
    plan = context.get("plan") or {}
    if plan.get("filePath") or plan.get("status"):
        lines.append("plan_state:")
        if plan.get("status"):
            lines.append(f"- status: {plan['status']}")
        if plan.get("filePath"):
            lines.append(f"- file: {plan['filePath']}")
    appendPromptList(lines, "open_todos", todos.get("open") or [])
    appendPromptList(lines, "recent_searches", activity.get("searches") or [])
    appendPromptList(lines, "recent_commands", activity.get("commands") or [])
    appendPromptList(lines, "recent_web", activity.get("web") or [])
    appendPromptList(lines, "recent_failures", activity.get("failures") or [])
    appendPromptList(lines, "next_steps", context.get("nextSteps") or [])
    appendPromptList(lines, "critical_context", context.get("criticalContext") or [])
    lines.append("</working_context>")
    return "\n".join(lines)


async def loadWorkingContextArtifact(filePath: str | None) -> dict[str, Any] | None:
    if not filePath:
        return None
    raw = _read_text(filePath)
    if not raw.strip():
        return None
    return coerceWorkingContextDocument(parseMaybeJson(raw))


def coerceWorkingContextDocument(value: Any) -> dict[str, Any] | None:
    record = asRecord(value)
    if not record or record.get("version") != "v2" or not isinstance(record.get("sessionId"), str):
        return None
    session = asRecord(record.get("session"))
    if not session or not isinstance(session.get("cwd"), str) or not isinstance(session.get("model"), str):
        return None
    project = asRecord(record.get("project")) or {}
    goals = asRecord(record.get("goals")) or {}
    current_state = asRecord(record.get("currentState")) or {}
    todos = asRecord(record.get("todos")) or {}
    activity = asRecord(record.get("activity")) or {}
    plan = asRecord(record.get("plan"))
    compaction = asRecord(record.get("compaction"))
    compaction_record = None
    if compaction and compaction.get("provider") == "openai" and isinstance(compaction.get("model"), str):
        compaction_record = {"provider": "openai", "model": compaction["model"]}
        if isinstance(compaction.get("effort"), str):
            compaction_record["effort"] = compaction["effort"]
    source_ranges = []
    if isinstance(record.get("sourceRanges"), list):
        for entry in record["sourceRanges"]:
            source = asRecord(entry)
            if not source:
                continue
            source_ranges.append(
                {
                    "generation": source.get("generation") if isinstance(source.get("generation"), int) else 1,
                    "start": source.get("start") if isinstance(source.get("start"), int) else 0,
                    "end": source.get("end") if isinstance(source.get("end"), int) else 0,
                    "messageCount": source.get("messageCount") if isinstance(source.get("messageCount"), int) else 0,
                    "createdAt": source.get("createdAt") if isinstance(source.get("createdAt"), str) else now_iso(),
                }
            )
    return {
        "version": "v2",
        "sessionId": record["sessionId"],
        "generatedAt": record.get("generatedAt") if isinstance(record.get("generatedAt"), str) else now_iso(),
        "updatedAt": record.get("updatedAt") if isinstance(record.get("updatedAt"), str) else now_iso(),
        "generation": record.get("generation") if isinstance(record.get("generation"), int) else 1,
        "compaction": compaction_record,
        "sourceRanges": source_ranges,
        "session": {
            "cwd": session["cwd"],
            "provider": "openai" if session.get("provider") == "openai" else "anthropic",
            "model": session["model"],
            "effort": session.get("effort") if isinstance(session.get("effort"), str) else None,
            "permissionMode": session.get("permissionMode") if isinstance(session.get("permissionMode"), str) else "default",
        },
        "project": {"description": project.get("description") if isinstance(project.get("description"), str) else None, "facts": coerceProjectFacts(project.get("facts")), "keyFiles": coerceRelevantFiles(project.get("keyFiles"))},
        "goals": {"active": asStringArray(goals.get("active")), "recentRequests": asStringArray(goals.get("recentRequests")), "constraints": asStringArray(goals.get("constraints")), "preferences": asStringArray(goals.get("preferences"))},
        "currentState": {"completed": asStringArray(current_state.get("completed")), "inProgress": asStringArray(current_state.get("inProgress")), "blocked": asStringArray(current_state.get("blocked"))},
        "decisions": asStringArray(record.get("decisions")),
        "discoveries": asStringArray(record.get("discoveries")),
        "plan": {
            "requestId": plan.get("requestId") if plan and isinstance(plan.get("requestId"), str) else None,
            "status": plan.get("status") if plan and isinstance(plan.get("status"), str) else None,
            "filePath": plan.get("filePath") if plan and isinstance(plan.get("filePath"), str) else None,
            "exists": bool(plan and plan.get("exists")),
            "content": plan.get("content") if plan and isinstance(plan.get("content"), str) else None,
        }
        if plan
        else None,
        "todos": {"open": asStringArray(todos.get("open")), "completed": asStringArray(todos.get("completed"))},
        "activity": {"commands": asStringArray(activity.get("commands")), "searches": asStringArray(activity.get("searches")), "web": asStringArray(activity.get("web")), "failures": asStringArray(activity.get("failures"))},
        "nextSteps": asStringArray(record.get("nextSteps")),
        "criticalContext": asStringArray(record.get("criticalContext")),
    }


def coerceSynthesizedWorkingContextFields(value: Any) -> dict[str, Any] | None:
    record = asRecord(value)
    if not record:
        return None
    project = asRecord(record.get("project")) or {}
    goals = asRecord(record.get("goals")) or {}
    current_state = asRecord(record.get("currentState")) or {}
    activity = asRecord(record.get("activity")) or {}
    return {
        "project": {
            "description": collapseWhitespace(project["description"]) if isinstance(project.get("description"), str) else None,
            "facts": coerceProjectFacts(project.get("facts")),
            "keyFiles": coerceRelevantFiles(project.get("keyFiles"))[:18],
        },
        "goals": {
            "active": asStringArray(goals.get("active"))[:8],
            "recentRequests": asStringArray(goals.get("recentRequests"))[:10],
            "constraints": asStringArray(goals.get("constraints"))[:10],
            "preferences": asStringArray(goals.get("preferences"))[:10],
        },
        "currentState": {
            "completed": asStringArray(current_state.get("completed"))[:16],
            "inProgress": asStringArray(current_state.get("inProgress"))[:12],
            "blocked": asStringArray(current_state.get("blocked"))[:10],
        },
        "decisions": asStringArray(record.get("decisions"))[:12],
        "discoveries": asStringArray(record.get("discoveries"))[:14],
        "activity": {
            "commands": asStringArray(activity.get("commands"))[:MAX_ACTIVITY_LINES],
            "searches": asStringArray(activity.get("searches"))[:MAX_ACTIVITY_LINES],
            "web": asStringArray(activity.get("web"))[:MAX_ACTIVITY_LINES],
            "failures": asStringArray(activity.get("failures"))[:MAX_FAILURES],
        },
        "nextSteps": asStringArray(record.get("nextSteps"))[:8],
        "criticalContext": asStringArray(record.get("criticalContext"))[:16],
    }


def coerceRepairedCheckpoint(value: Any) -> dict[str, Any] | None:
    record = asRecord(value)
    if not record:
        return None
    quality = asRecord(record.get("quality")) or {}
    classification = quality.get("classification") if quality.get("classification") in {"thin", "usable", "rich"} else "usable"
    reason = collapseWhitespace(quality.get("reason")) if isinstance(quality.get("reason"), str) else "Checkpoint repaired by verifier."
    working_context = coerceSynthesizedWorkingContextFields(record.get("workingContext"))
    if not working_context:
        return None
    return {"qualityClassification": classification, "qualityReason": reason, "workingContext": working_context}


def coerceProjectFacts(value: Any) -> dict[str, Any] | None:
    record = asRecord(value)
    if not record:
        return None
    languages = asStringArray(record.get("languages"))
    evidence = asStringArray(record.get("evidence"))
    if not any([isinstance(record.get("primaryLanguage"), str), isinstance(record.get("ecosystem"), str), isinstance(record.get("packageManager"), str), languages, evidence]):
        return None
    return {
        "primaryLanguage": record.get("primaryLanguage") if isinstance(record.get("primaryLanguage"), str) else None,
        "languages": languages,
        "ecosystem": record.get("ecosystem") if isinstance(record.get("ecosystem"), str) else None,
        "packageManager": record.get("packageManager") if isinstance(record.get("packageManager"), str) else None,
        "evidence": evidence,
    }


def coerceRelevantFiles(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    files = []
    for entry in value:
        record = asRecord(entry)
        if record and isinstance(record.get("path"), str):
            files.append({"path": str(record["path"]), "role": "modified" if record.get("role") == "modified" else "read", "note": record.get("note") if isinstance(record.get("note"), str) else None})
    return files


def assertModelBackedCompactionArtifacts(stateSnapshot: dict[str, Any], workingContext: dict[str, Any]) -> None:
    if not asRecord(stateSnapshot.get("compactionModel")) or not asRecord(stateSnapshot.get("synthesis")):
        raise RuntimeError("Compaction state snapshot is not model-backed; refusing to persist checkpoint.")
    if not asRecord(stateSnapshot.get("verificationModel")) or not asRecord(stateSnapshot.get("verification")):
        raise RuntimeError("Compaction state snapshot is not verifier-backed; refusing to persist checkpoint.")
    if not workingContext.get("compaction"):
        raise RuntimeError("Working context is missing compaction model metadata; refusing to persist checkpoint.")


def normalizeVerificationVerdict(value: Any) -> str | None:
    return value if value in {"accept", "accept_with_notes", "repair", "reject"} else None


def normalizeVerificationConfidence(value: Any) -> str:
    return value if value in {"low", "medium", "high"} else "medium"


def shouldRetryCompactionAttempt(error: BaseException, hasMoreAttempts: bool) -> bool:
    if not hasMoreAttempts:
        return False
    return fallbackEligibleProviderFailure(error) or isCompactionContextSizeFailure(error)


def isCompactionContextSizeFailure(error: BaseException) -> bool:
    if not isProviderRequestError(error):
        return False
    record = getattr(error, "record", {})
    if record.get("category") not in {"client_request", "protocol"}:
        return False
    detail = f"{record.get('rawMessage') or ''} {record.get('rawBody') or ''} {error}".lower()
    return bool(re.search(r"context|input token|prompt token|max token|too large|too long|context_length|request exceeds|input is too long", detail))


def parseMaybeJson(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if not trimmed or (not trimmed.startswith("{") and not trimmed.startswith("[")):
        return value
    try:
        return json.loads(trimmed)
    except Exception:
        return value


def estimateMessageTokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimateSingleMessageTokens(message) for message in messages)


def estimateSingleMessageTokens(message: dict[str, Any]) -> int:
    content = message.get("content")
    value = f"{message.get('role')}:{content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, separators=(',', ':'))}"
    return estimateTextTokens(value)


def estimateTextTokens(value: str) -> int:
    return (len(value) + 3) // 4


def _estimate_text_tokens(value: str) -> int:
    return estimateTextTokens(value)


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return estimateMessageTokens(messages)


def asRecord(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def asStringArray(value: Any) -> list[str]:
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []


def collapseWhitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def trimPathEvidence(filePath: str, cwd: str) -> str:
    normalized = cwd if cwd.endswith(os.sep) else f"{cwd}{os.sep}"
    return filePath[len(normalized) :] if filePath.startswith(normalized) else filePath


def summarizeTodo(todo: dict[str, Any]) -> str:
    content = collapseWhitespace(todo.get("content", ""))
    active_form = collapseWhitespace(todo.get("activeForm", ""))
    preferred = content if isLowSignalTodoText(active_form) else active_form
    return truncate(preferred or content or active_form or "todo", 160)


def isLowSignalTodoText(text: str) -> bool:
    return bool(re.match(r"^(coding|working|building|implementing|reviewing|testing|debugging|planning)$", text.strip(), re.IGNORECASE))


def appendProjectFactsSection(lines: list[str], context: dict[str, Any]) -> None:
    project = context.get("project") or {}
    facts = project.get("facts") or {}
    if not facts and not project.get("description"):
        return
    lines.extend(["", "## Project Facts", ""])
    if project.get("description"):
        lines.append(f"- project: {project['description']}")
    if facts.get("primaryLanguage"):
        lines.append(f"- primary language: `{facts['primaryLanguage']}`")
    if facts.get("languages"):
        lines.append(f"- detected languages: {', '.join(f'`{value}`' for value in facts['languages'])}")
    if facts.get("ecosystem"):
        lines.append(f"- ecosystem: `{facts['ecosystem']}`")
    if facts.get("packageManager"):
        lines.append(f"- package manager: `{facts['packageManager']}`")
    if facts.get("evidence"):
        lines.append(f"- evidence: {', '.join(f'`{value}`' for value in facts['evidence'])}")


def appendStringSection(lines: list[str], heading: str, entries: list[str]) -> None:
    values = [entry for entry in entries if entry]
    if not values:
        return
    lines.extend(["", f"## {heading}", ""])
    lines.extend(f"- {entry}" for entry in values)


def appendSubsection(lines: list[str], heading: str, entries: list[str]) -> None:
    lines.append(f"### {heading}")
    if not entries:
        lines.extend(["- None.", ""])
        return
    lines.extend(f"- {entry}" for entry in entries)
    lines.append("")


def appendRelevantFilesSection(lines: list[str], files: list[dict[str, Any]]) -> None:
    if not files:
        return
    lines.extend(["", "## Relevant Files", ""])
    for file_record in files:
        note = f" — {file_record['note']}" if file_record.get("note") else ""
        lines.append(f"- `{file_record.get('path')}`{note}")


def appendPlanSection(lines: list[str], context: dict[str, Any]) -> None:
    plan = context.get("plan") or {}
    if not plan.get("filePath") and not plan.get("status"):
        return
    lines.extend(["", "## Plan State", ""])
    if plan.get("status"):
        lines.append(f"- status: `{plan['status']}`")
    if plan.get("filePath"):
        lines.append(f"- file: `{plan['filePath']}`")


def appendPromptList(lines: list[str], heading: str, entries: list[str]) -> None:
    values = [entry for entry in entries if entry]
    if not values:
        return
    lines.append(f"{heading}:")
    lines.extend(f"- {entry}" for entry in values)


def appendActivitySection(lines: list[str], heading: str, entries: list[str]) -> None:
    if not entries:
        return
    lines.extend([f"## {heading}", ""])
    lines.extend(entries)
    lines.append("")


def _format_relevant_file_prompt(record: dict[str, Any]) -> str:
    note = f" ({record.get('note')})" if record.get("note") else ""
    return f"{record.get('path')}{note}"


async def _call_model_client(model_client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    complete = model_client.get("complete") if isinstance(model_client, dict) else getattr(model_client, "complete", None)
    if complete is None:
        complete = model_client["complete"]
    return await complete(payload)


def _model_record(model: dict[str, Any]) -> dict[str, Any]:
    return {"provider": model.get("provider"), "model": model.get("model"), "effort": model.get("effort"), "selectionSource": model.get("selectionSource")}


def _verification_record(verification: dict[str, Any]) -> dict[str, Any]:
    return {"verdict": verification.get("verdict"), "confidence": verification.get("confidence"), "reason": verification.get("reason"), "issues": verification.get("issues") or [], "notes": verification.get("notes") or []}


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def _write_text(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value)


def _write_json(path: str, value: Any) -> None:
    _write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")
