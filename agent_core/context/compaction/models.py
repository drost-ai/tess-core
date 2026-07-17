from __future__ import annotations

import math

from .shared import *
from ...provider_token_budget import DEFAULT_COMPACTION_PRESERVATION_RATIO, resolveCompactionRequestOutputTokens
from ...usage_costs import mergeProviderUsage as _merge_provider_usage
from ..compaction_models import resolveCompactionTokenPolicy


def buildCompactionAttemptStrategies(modelCandidates: list[dict[str, Any]], options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    seen: set[str] = set()
    budget_for_model = (options or {}).get("evidenceTokenBudgetForModel")
    for model in modelCandidates:
        preferred_budget = _normalize_evidence_token_budget(budget_for_model(model) if callable(budget_for_model) else None)
        evidence_budgets = [budget for budget in [preferred_budget, *COMPACTION_EVIDENCE_FALLBACK_BUDGETS] if isinstance(budget, int)]
        for evidence_budget in evidence_budgets:
            key = ":".join([str(model.get("purpose")), str(model.get("provider")), str(model.get("model")), str(model.get("effort") or ""), str(evidence_budget)])
            if key in seen:
                continue
            seen.add(key)
            attempts.append({"model": model, "evidenceTokenBudget": evidence_budget})
    return attempts


async def runModelBackedCompactionSynthesisWithFallback(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    candidates = resolveCompactionModelCandidates({"config": args.get("config") or {}, "session": args["session"], "purpose": "synthesis"})
    source_token_estimate = estimateMessageTokens(args["sourceMessages"])
    attempts = buildCompactionAttemptStrategies(
        candidates,
        {
            "evidenceTokenBudgetForModel": lambda model: resolveCompactionRequestOutputTokens(
                {"profile": resolveCompactionTokenPolicy({"config": args.get("config") or {}, "model": model}), "sourceTokenEstimate": source_token_estimate}
            )
        },
    )
    last_error: Exception | None = None
    for index, attempt in enumerate(attempts):
        try:
            return await runModelBackedCompactionSynthesis({**args, "model": attempt["model"], "evidenceTokenBudget": attempt["evidenceTokenBudget"]})
        except Exception as exc:
            last_error = exc
            if not shouldRetryCompactionAttempt(exc, index < len(attempts) - 1):
                if shouldSkipCompactionSynthesisFailure(exc):
                    return buildSkippedCompactionSynthesis(
                        {
                            "session": args["session"],
                            "previousWorkingContext": args.get("previousWorkingContext"),
                            "sourceMessages": args["sourceMessages"],
                            "sourceStart": int(args.get("sourceStart") or 0),
                            "model": attempt["model"],
                            "evidenceTokenBudget": attempt["evidenceTokenBudget"],
                            "reason": describeCompactionSynthesisFailure(exc),
                        }
                    )
                raise
    if shouldSkipCompactionSynthesisFailure(last_error):
        final_attempt = attempts[-1] if attempts else None
        if final_attempt:
            return buildSkippedCompactionSynthesis(
                {
                    "session": args["session"],
                    "previousWorkingContext": args.get("previousWorkingContext"),
                    "sourceMessages": args["sourceMessages"],
                    "sourceStart": int(args.get("sourceStart") or 0),
                    "model": final_attempt["model"],
                    "evidenceTokenBudget": final_attempt["evidenceTokenBudget"],
                    "reason": describeCompactionSynthesisFailure(last_error),
                }
            )
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
    last_raw_response_text = ""
    usage: dict[str, Any] = {}
    source_token_estimate = estimateMessageTokens(args["sourceMessages"])
    token_policy = resolveCompactionTokenPolicy({"config": args.get("config") or {}, "model": model})
    output_token_budget = resolveCompactionRequestOutputTokens({"profile": token_policy, "sourceTokenEstimate": source_token_estimate})
    target_preservation_tokens = math.ceil(source_token_estimate * DEFAULT_COMPACTION_PRESERVATION_RATIO)
    for attempt in range(1, COMPACTION_SYNTHESIS_MAX_RETRIES + 1):
        response = await _call_model_client(
            args["modelClient"],
            {
                "config": args.get("config") or {},
                "runtime": args.get("runtime") or {},
                "abortSignal": args.get("abortSignal"),
                "model": model,
                "system": COMPACTION_SYSTEM_PROMPT,
                "maxOutputTokens": output_token_budget,
                "prompt": buildCompactionSynthesisPrompt(
                    {
                        "session": session,
                        "model": model,
                        "previousWorkingContext": args.get("previousWorkingContext"),
                        "latestPlan": args.get("latestPlan"),
                        "sourceStart": int(args.get("sourceStart") or 0),
                        "sourceEnd": int(args.get("sourceStart") or 0) + len(args["sourceMessages"]),
                        "evidence": evidence,
                        "sourceTokenEstimate": source_token_estimate,
                        "outputTokenBudget": output_token_budget,
                        "targetPreservationTokens": target_preservation_tokens,
                        "previousError": last_error if attempt > 1 else None,
                    }
                ),
            },
        )
        raw_response_text = str(response.get("text") or "").strip()
        last_raw_response_text = raw_response_text
        _merge_compaction_model_usage(usage, response.get("usage") or {})
        parsed = parseCompactionSynthesisResponse(raw_response_text)
        if parsed:
            return {**parsed, "usage": usage, "rawResponseText": raw_response_text, "evidence": evidence, "evidenceBudgetTokens": evidence_budget, "model": response.get("model") or model}
        last_error = f"Attempt {attempt} returned invalid compaction JSON: {truncate(raw_response_text, 1200)}"
    return buildEvidenceBackedCompactionSynthesis(
        {
            "session": session,
            "previousWorkingContext": args.get("previousWorkingContext"),
            "latestPlan": args.get("latestPlan"),
            "sourceMessages": args["sourceMessages"],
            "sourceStart": int(args.get("sourceStart") or 0),
            "evidence": evidence,
            "evidenceBudgetTokens": evidence_budget,
            "model": model,
            "usage": usage,
            "rawResponseText": last_raw_response_text,
            "reason": f"Compaction synthesis returned malformed JSON after {COMPACTION_SYNTHESIS_MAX_RETRIES} attempts. {last_error}",
        }
    )


async def runCompactionCheckpointVerificationWithFallback(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    candidates = resolveCompactionModelCandidates({"config": args.get("config") or {}, "session": args["session"], "purpose": "verification"})
    source_token_estimate = estimateMessageTokens(args["sourceMessages"])
    attempts = buildCompactionAttemptStrategies(
        candidates,
        {
            "evidenceTokenBudgetForModel": lambda model: resolveCompactionRequestOutputTokens(
                {"profile": resolveCompactionTokenPolicy({"config": args.get("config") or {}, "model": model}), "sourceTokenEstimate": source_token_estimate}
            )
        },
    )
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
    last_invalid_response: dict[str, Any] | None = None
    last_invalid_raw_response_text = ""
    source_token_estimate = estimateMessageTokens(args["sourceMessages"])
    token_policy = resolveCompactionTokenPolicy({"config": args.get("config") or {}, "model": model})
    output_token_budget = resolveCompactionRequestOutputTokens({"profile": token_policy, "sourceTokenEstimate": source_token_estimate})
    target_preservation_tokens = math.ceil(source_token_estimate * DEFAULT_COMPACTION_PRESERVATION_RATIO)
    for attempt in range(1, COMPACTION_VERIFICATION_MAX_RETRIES + 1):
        response = await _call_model_client(
            args["modelClient"],
            {
                "config": args.get("config") or {},
                "runtime": args.get("runtime") or {},
                "abortSignal": args.get("abortSignal"),
                "model": model,
                "system": COMPACTION_VERIFICATION_SYSTEM_PROMPT,
                "maxOutputTokens": output_token_budget,
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
                        "sourceTokenEstimate": source_token_estimate,
                        "outputTokenBudget": output_token_budget,
                        "targetPreservationTokens": target_preservation_tokens,
                        "previousError": last_error if attempt > 1 else None,
                    }
                ),
            },
        )
        raw_response_text = str(response.get("text") or "").strip()
        parsed = parseCompactionVerificationResponse(raw_response_text)
        if parsed:
            return {**parsed, "usage": response.get("usage") or {}, "rawResponseText": raw_response_text, "evidence": evidence, "evidenceBudgetTokens": evidence_budget, "model": response.get("model") or model}
        last_invalid_response = response
        last_invalid_raw_response_text = raw_response_text
        last_error = f"Attempt {attempt} returned invalid compaction verification JSON: {truncate(raw_response_text, 1200)}"
    if last_invalid_response:
        return {
            "verdict": "reject",
            "confidence": "low",
            "reason": f"Compaction verification did not return valid verification JSON after {COMPACTION_VERIFICATION_MAX_RETRIES} attempts; rejecting the candidate checkpoint instead. {last_error}",
            "issues": [last_error],
            "notes": [],
            "usage": last_invalid_response.get("usage") or {},
            "rawResponseText": last_invalid_raw_response_text,
            "evidence": evidence,
            "evidenceBudgetTokens": evidence_budget,
            "model": last_invalid_response.get("model") or model,
        }
    raise RuntimeError(f"Compaction verification failed: {last_error}")


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


def shouldSkipCompactionSynthesisFailure(error: BaseException | None) -> bool:
    return bool(error and (fallbackEligibleProviderFailure(error) or isCompactionContextSizeFailure(error) or isCompactionSynthesisJsonFailure(error)))


def isCompactionSynthesisJsonFailure(error: BaseException | None) -> bool:
    return isinstance(error, RuntimeError) and str(error).startswith("Compaction synthesis failed:")


def describeCompactionSynthesisFailure(error: BaseException | None) -> str:
    if error is None:
        return "Compaction synthesis failed: unknown error"
    if isProviderRequestError(error):
        record = getattr(error, "record", {})
        detail = f"{record.get('providerLabel', 'Provider')} compaction synthesis failed with {record.get('category')}: {record.get('message')}"
        if record.get("rawMessage"):
            detail += f" Raw provider detail: {truncate(str(record.get('rawMessage')), 500)}"
        return detail
    return f"Compaction synthesis failed: {error}"


def buildSkippedCompactionSynthesis(args: dict[str, Any]) -> dict[str, Any]:
    evidence_budget = int(args.get("evidenceTokenBudget") or COMPACTION_EVIDENCE_TOKEN_BUDGET)
    evidence = buildCompactionEvidence(
        {
            "sourceMessages": args["sourceMessages"],
            "sourceStart": int(args.get("sourceStart") or 0),
            "cwd": args["session"].get("cwd") or os.getcwd(),
            "tokenBudget": evidence_budget,
        }
    )
    quality_reason = f"{args.get('reason')} Checkpoint creation was skipped so the session can continue and retry compaction later."
    return {
        "shouldCheckpoint": False,
        "qualityClassification": "thin",
        "qualityReason": quality_reason,
        "workingContext": buildFallbackSynthesizedWorkingContext(args.get("previousWorkingContext"), quality_reason),
        "usage": {},
        "rawResponseText": "",
        "evidence": evidence,
        "evidenceBudgetTokens": evidence_budget,
        "model": args["model"],
    }


def buildFallbackSynthesizedWorkingContext(previous: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    previous = previous or {}
    previous_state = previous.get("currentState") or {}
    previous_activity = previous.get("activity") or {}
    return {
        "project": previous.get("project") or {"keyFiles": []},
        "goals": previous.get("goals") or {"active": [], "recentRequests": [], "constraints": [], "preferences": []},
        "currentState": {
            "completed": previous_state.get("completed") or [],
            "inProgress": previous_state.get("inProgress") or [],
            "blocked": [*(previous_state.get("blocked") or []), reason][-10:],
        },
        "decisions": previous.get("decisions") or [],
        "discoveries": previous.get("discoveries") or [],
        "activity": {
            "commands": previous_activity.get("commands") or [],
            "searches": previous_activity.get("searches") or [],
            "web": previous_activity.get("web") or [],
            "failures": [*(previous_activity.get("failures") or []), reason][-MAX_FAILURES:],
        },
        "nextSteps": previous.get("nextSteps") or [],
        "criticalContext": previous.get("criticalContext") or [],
    }


def buildEvidenceBackedCompactionSynthesis(args: dict[str, Any]) -> dict[str, Any]:
    source_start = int(args.get("sourceStart") or 0)
    source_end = source_start + len(args.get("sourceMessages") or [])
    reason = collapseWhitespace(str(args.get("reason") or "Compaction synthesis returned malformed JSON."))
    previous = args.get("previousWorkingContext") or {}
    previous_project = previous.get("project") or {}
    previous_goals = previous.get("goals") or {}
    previous_state = previous.get("currentState") or {}
    previous_activity = previous.get("activity") or {}
    recent_user_requests = _extract_recent_user_requests(args.get("sourceMessages") or [])
    evidence_files = _extract_evidence_file_references((args.get("evidence") or {}).get("transcript") or "")
    working_context = {
        "project": {
            "description": previous_project.get("description"),
            "facts": previous_project.get("facts") or {"languages": [], "evidence": [f"Deterministic preserved trace covers source messages {source_start}-{max(source_end - 1, source_start)}."]},
            "keyFiles": [*(previous_project.get("keyFiles") or []), *evidence_files][:96],
        },
        "goals": {
            "active": recent_user_requests[-3:] or previous_goals.get("active") or ["Continue from the recent raw transcript; use preserved trace for compacted history."],
            "recentRequests": dedupeStrings([*(previous_goals.get("recentRequests") or []), *recent_user_requests])[-48:],
            "constraints": previous_goals.get("constraints") or [],
            "preferences": previous_goals.get("preferences") or [],
        },
        "currentState": {
            "completed": previous_state.get("completed") or [],
            "inProgress": dedupeStrings([*(previous_state.get("inProgress") or []), "Compacted history is preserved as deterministic trace evidence because synthesis returned malformed JSON."])[-64:],
            "blocked": previous_state.get("blocked") or [],
        },
        "decisions": previous.get("decisions") or [],
        "discoveries": previous.get("discoveries") or [],
        "activity": {
            "commands": previous_activity.get("commands") or [],
            "searches": previous_activity.get("searches") or [],
            "web": previous_activity.get("web") or [],
            "failures": dedupeStrings([*(previous_activity.get("failures") or []), reason])[-MAX_FAILURES:],
        },
        "nextSteps": previous.get("nextSteps") or ["Use the recent raw messages for the immediate continuation state.", "Use preserved trace as authoritative evidence for compacted history."],
        "criticalContext": dedupeStrings(
            [
                *(previous.get("criticalContext") or []),
                f"Preserved trace is authoritative for compacted source messages {source_start}-{max(source_end - 1, source_start)}.",
                f"Evidence entries included: {args['evidence']['includedEntryCount']}/{args['evidence']['entryCount']}; evidence tokens: {args['evidence']['tokenEstimate']}/{args['evidence']['tokenBudget']}.",
                f"Latest plan file: {args['latestPlan']['filePath']}." if isinstance(args.get("latestPlan"), dict) and args["latestPlan"].get("filePath") else "",
            ]
        )[-128:],
    }
    return {
        "shouldCheckpoint": args["evidence"].get("includedEntryCount", 0) > 0,
        "qualityClassification": "usable",
        "qualityReason": f"{reason} Built a deterministic evidence-backed checkpoint instead of dropping compaction.",
        "workingContext": working_context,
        "usage": args.get("usage") or {},
        "rawResponseText": args.get("rawResponseText") or "",
        "evidence": args["evidence"],
        "evidenceBudgetTokens": args["evidenceBudgetTokens"],
        "model": args["model"],
    }


def _extract_recent_user_requests(messages: list[dict[str, Any]]) -> list[str]:
    requests: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        text = _extract_message_text(message)
        if text:
            requests.append(truncate(text, 600))
    return requests[-12:]


def _extract_message_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return collapseWhitespace(content) or None
    if isinstance(content, list):
        text = "\n\n".join(str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text")
        return collapseWhitespace(text) or None
    return None


def _extract_evidence_file_references(transcript: str) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(r"\bfile=([^\n]+)", transcript):
        value = match.group(1).strip()
        if value and value not in seen:
            seen.add(value)
            paths.append({"path": value, "role": "read", "note": "Referenced in deterministic compaction evidence."})
    return paths[:48]


def _merge_compaction_model_usage(target: dict[str, Any], usage: dict[str, Any]) -> None:
    _merge_provider_usage(target, usage)


def _normalize_evidence_token_budget(value: Any) -> int | None:
    try:
        parsed = int(float(value))
    except Exception:
        return None
    return parsed if parsed > 0 else None


async def _call_model_client(model_client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    complete = model_client.get("complete") if isinstance(model_client, dict) else getattr(model_client, "complete", None)
    if complete is None:
        complete = model_client["complete"]
    return await complete(payload)


def _model_record(model: dict[str, Any]) -> dict[str, Any]:
    return {"provider": model.get("provider"), "model": model.get("model"), "effort": model.get("effort"), "selectionSource": model.get("selectionSource")}


def _verification_record(verification: dict[str, Any]) -> dict[str, Any]:
    return {"verdict": verification.get("verdict"), "confidence": verification.get("confidence"), "reason": verification.get("reason"), "issues": verification.get("issues") or [], "notes": verification.get("notes") or []}
