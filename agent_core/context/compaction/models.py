from __future__ import annotations

from .shared import *


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


async def _call_model_client(model_client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    complete = model_client.get("complete") if isinstance(model_client, dict) else getattr(model_client, "complete", None)
    if complete is None:
        complete = model_client["complete"]
    return await complete(payload)


def _model_record(model: dict[str, Any]) -> dict[str, Any]:
    return {"provider": model.get("provider"), "model": model.get("model"), "effort": model.get("effort"), "selectionSource": model.get("selectionSource")}


def _verification_record(verification: dict[str, Any]) -> dict[str, Any]:
    return {"verdict": verification.get("verdict"), "confidence": verification.get("confidence"), "reason": verification.get("reason"), "issues": verification.get("issues") or [], "notes": verification.get("notes") or []}
