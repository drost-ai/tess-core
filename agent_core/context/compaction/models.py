from __future__ import annotations

import math

import os
import re
from ...providers.errors import fallback_eligible_provider_failure, is_provider_request_error
from ..compaction_transport import resolve_compaction_model_candidates
from .evidence import build_compaction_evidence
from .parsing import parse_compaction_synthesis_response, parse_compaction_verification_response
from .prompts import build_compaction_synthesis_prompt, build_compaction_verification_prompt
from .shared import (
    compaction_evidence_fallback_budgets,
    compaction_knob,
    COMPACTION_SYSTEM_PROMPT,
    COMPACTION_VERIFICATION_SYSTEM_PROMPT,
    MAX_FAILURES,
)
from .utils import collapse_whitespace, estimate_message_tokens, truncate
from typing import Any
from .artifacts import merge_compaction_usage_into_session
from ...storage.message_origin import is_genuine_user_request as _is_genuine_user_request
from ...providers._knob_reader import provider_knob
from ...providers.token_budget import resolveCompactionRequestOutputTokens
from ...providers.usage_costs import merge_provider_usage as _merge_provider_usage
from ..compaction_transport import resolve_compaction_token_policy


def build_compaction_attempt_strategies(modelCandidates: list[dict[str, Any]], options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    seen: set[str] = set()
    budget_for_model = (options or {}).get("evidenceTokenBudgetForModel")
    for model in modelCandidates:
        preferred_budget = _normalize_evidence_token_budget(budget_for_model(model) if callable(budget_for_model) else None)
        evidence_budgets = [budget for budget in [preferred_budget, *compaction_evidence_fallback_budgets()] if isinstance(budget, int)]
        for evidence_budget in evidence_budgets:
            key = ":".join([str(model.get("purpose")), str(model.get("provider")), str(model.get("model")), str(model.get("effort") or ""), str(evidence_budget)])
            if key in seen:
                continue
            seen.add(key)
            attempts.append({"model": model, "evidenceTokenBudget": evidence_budget})
    return attempts


async def run_model_backed_compaction_synthesis_with_fallback(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    candidates = resolve_compaction_model_candidates({"config": args.get("config") or {}, "session": args["session"], "purpose": "synthesis"})
    source_token_estimate = estimate_message_tokens(args["sourceMessages"])
    attempts = build_compaction_attempt_strategies(
        candidates,
        {
            "evidenceTokenBudgetForModel": lambda model: resolveCompactionRequestOutputTokens(
                {"profile": resolve_compaction_token_policy({"config": args.get("config") or {}, "model": model}), "sourceTokenEstimate": source_token_estimate}
            )
        },
    )
    last_error: Exception | None = None
    for index, attempt in enumerate(attempts):
        try:
            return await run_model_backed_compaction_synthesis({**args, "model": attempt["model"], "evidenceTokenBudget": attempt["evidenceTokenBudget"]})
        except Exception as exc:
            last_error = exc
            if not should_retry_compaction_attempt(exc, index < len(attempts) - 1):
                if should_skip_compaction_synthesis_failure(exc):
                    return build_skipped_compaction_synthesis(
                        {
                            "session": args["session"],
                            "previousWorkingContext": args.get("previousWorkingContext"),
                            "sourceMessages": args["sourceMessages"],
                            "sourceStart": int(args.get("sourceStart") or 0),
                            "model": attempt["model"],
                            "evidenceTokenBudget": attempt["evidenceTokenBudget"],
                            "reason": describe_compaction_synthesis_failure(exc),
                        }
                    )
                raise
    if should_skip_compaction_synthesis_failure(last_error):
        final_attempt = attempts[-1] if attempts else None
        if final_attempt:
            return build_skipped_compaction_synthesis(
                {
                    "session": args["session"],
                    "previousWorkingContext": args.get("previousWorkingContext"),
                    "sourceMessages": args["sourceMessages"],
                    "sourceStart": int(args.get("sourceStart") or 0),
                    "model": final_attempt["model"],
                    "evidenceTokenBudget": final_attempt["evidenceTokenBudget"],
                    "reason": describe_compaction_synthesis_failure(last_error),
                }
            )
    raise last_error or RuntimeError("Compaction synthesis failed.")


async def run_model_backed_compaction_synthesis(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    session = args["session"]
    model = args["model"]
    evidence_budget = int(args.get("evidenceTokenBudget") or compaction_knob("compaction.evidence_token_budget"))
    evidence = build_compaction_evidence(
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
    source_token_estimate = estimate_message_tokens(args["sourceMessages"])
    token_policy = resolve_compaction_token_policy({"config": args.get("config") or {}, "model": model})
    output_token_budget = resolveCompactionRequestOutputTokens({"profile": token_policy, "sourceTokenEstimate": source_token_estimate})
    target_preservation_tokens = math.ceil(source_token_estimate * float(provider_knob("provider.compaction_preservation_ratio")))
    for attempt in range(1, compaction_knob("compaction.synthesis_max_retries") + 1):
        response = await _call_model_client(
            args["modelClient"],
            {
                "config": args.get("config") or {},
                "runtime": args.get("runtime") or {},
                "abortSignal": args.get("abortSignal"),
                "model": model,
                "sessionId": session.get("id"),
                "system": COMPACTION_SYSTEM_PROMPT,
                "maxOutputTokens": output_token_budget,
                "prompt": build_compaction_synthesis_prompt(
                    {
                        "session": session,
                        "model": model,
                        "previousWorkingContext": args.get("previousWorkingContext"),
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
        merge_compaction_usage_into_session(session, response.get("usage") or {})
        parsed = parse_compaction_synthesis_response(raw_response_text)
        if parsed:
            return {**parsed, "usage": usage, "rawResponseText": raw_response_text, "evidence": evidence, "evidenceBudgetTokens": evidence_budget, "model": response.get("model") or model}
        last_error = f"Attempt {attempt} returned invalid compaction JSON: {truncate(raw_response_text, 1200)}"
    return build_evidence_backed_compaction_synthesis(
        {
            "session": session,
            "previousWorkingContext": args.get("previousWorkingContext"),
            "sourceMessages": args["sourceMessages"],
            "sourceStart": int(args.get("sourceStart") or 0),
            "evidence": evidence,
            "evidenceBudgetTokens": evidence_budget,
            "model": model,
            "usage": usage,
            "rawResponseText": last_raw_response_text,
            "reason": f"Compaction synthesis returned malformed JSON after {compaction_knob("compaction.synthesis_max_retries")} attempts. {last_error}",
        }
    )


async def run_compaction_checkpoint_verification_with_fallback(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    candidates = resolve_compaction_model_candidates({"config": args.get("config") or {}, "session": args["session"], "purpose": "verification"})
    source_token_estimate = estimate_message_tokens(args["sourceMessages"])
    attempts = build_compaction_attempt_strategies(
        candidates,
        {
            "evidenceTokenBudgetForModel": lambda model: resolveCompactionRequestOutputTokens(
                {"profile": resolve_compaction_token_policy({"config": args.get("config") or {}, "model": model}), "sourceTokenEstimate": source_token_estimate}
            )
        },
    )
    last_error: Exception | None = None
    for index, attempt in enumerate(attempts):
        try:
            return await run_compaction_checkpoint_verification({**args, "model": attempt["model"], "evidenceTokenBudget": attempt["evidenceTokenBudget"]})
        except Exception as exc:
            last_error = exc
            if not should_retry_compaction_attempt(exc, index < len(attempts) - 1):
                raise
    raise last_error or RuntimeError("Compaction verification failed.")


async def run_compaction_checkpoint_verification(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    session = args["session"]
    model = args["model"]
    evidence_budget = int(args.get("evidenceTokenBudget") or compaction_knob("compaction.evidence_token_budget"))
    source_start = int(args.get("sourceStart") or 0)
    evidence = build_compaction_evidence(
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
    usage: dict[str, Any] = {}
    source_token_estimate = estimate_message_tokens(args["sourceMessages"])
    token_policy = resolve_compaction_token_policy({"config": args.get("config") or {}, "model": model})
    output_token_budget = resolveCompactionRequestOutputTokens({"profile": token_policy, "sourceTokenEstimate": source_token_estimate})
    target_preservation_tokens = math.ceil(source_token_estimate * float(provider_knob("provider.compaction_preservation_ratio")))
    for attempt in range(1, compaction_knob("compaction.verification_max_retries") + 1):
        response = await _call_model_client(
            args["modelClient"],
            {
                "config": args.get("config") or {},
                "runtime": args.get("runtime") or {},
                "abortSignal": args.get("abortSignal"),
                "model": model,
                "sessionId": session.get("id"),
                "system": COMPACTION_VERIFICATION_SYSTEM_PROMPT,
                "maxOutputTokens": output_token_budget,
                "prompt": build_compaction_verification_prompt(
                    {
                        "session": session,
                        "model": model,
                        "previousWorkingContext": args.get("previousWorkingContext"),
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
        _merge_compaction_model_usage(usage, response.get("usage") or {})
        merge_compaction_usage_into_session(session, response.get("usage") or {})
        parsed = parse_compaction_verification_response(raw_response_text)
        if parsed:
            return {**parsed, "usage": usage, "rawResponseText": raw_response_text, "evidence": evidence, "evidenceBudgetTokens": evidence_budget, "model": response.get("model") or model}
        last_invalid_response = response
        last_invalid_raw_response_text = raw_response_text
        last_error = f"Attempt {attempt} returned invalid compaction verification JSON: {truncate(raw_response_text, 1200)}"
    if last_invalid_response:
        return {
            "verdict": "reject",
            "confidence": "low",
            "reason": f"Compaction verification did not return valid verification JSON after {compaction_knob("compaction.verification_max_retries")} attempts; rejecting the candidate checkpoint instead. {last_error}",
            "issues": [last_error],
            "notes": [],
            "usage": usage,
            "rawResponseText": last_invalid_raw_response_text,
            "evidence": evidence,
            "evidenceBudgetTokens": evidence_budget,
            "model": last_invalid_response.get("model") or model,
        }
    raise RuntimeError(f"Compaction verification failed: {last_error}")


def should_retry_compaction_attempt(error: BaseException, hasMoreAttempts: bool) -> bool:
    if not hasMoreAttempts:
        return False
    return fallback_eligible_provider_failure(error) or is_compaction_context_size_failure(error)


def is_compaction_context_size_failure(error: BaseException) -> bool:
    if not is_provider_request_error(error):
        return False
    record = getattr(error, "record", {})
    if record.get("category") not in {"client_request", "protocol"}:
        return False
    detail = f"{record.get('rawMessage') or ''} {record.get('rawBody') or ''} {error}".lower()
    return bool(re.search(r"context|input token|prompt token|max token|too large|too long|context_length|request exceeds|input is too long", detail))


def should_skip_compaction_synthesis_failure(error: BaseException | None) -> bool:
    return bool(error and (fallback_eligible_provider_failure(error) or is_compaction_context_size_failure(error) or is_compaction_synthesis_json_failure(error)))


def is_compaction_synthesis_json_failure(error: BaseException | None) -> bool:
    return isinstance(error, RuntimeError) and str(error).startswith("Compaction synthesis failed:")


def describe_compaction_synthesis_failure(error: BaseException | None) -> str:
    if error is None:
        return "Compaction synthesis failed: unknown error"
    if is_provider_request_error(error):
        record = getattr(error, "record", {})
        detail = f"{record.get('providerLabel', 'Provider')} compaction synthesis failed with {record.get('category')}: {record.get('message')}"
        if record.get("rawMessage"):
            detail += f" Raw provider detail: {truncate(str(record.get('rawMessage')), 500)}"
        return detail
    return f"Compaction synthesis failed: {error}"


def build_skipped_compaction_synthesis(args: dict[str, Any]) -> dict[str, Any]:
    evidence_budget = int(args.get("evidenceTokenBudget") or compaction_knob("compaction.evidence_token_budget"))
    evidence = build_compaction_evidence(
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
        "workingContext": build_fallback_synthesized_working_context(args.get("previousWorkingContext"), quality_reason),
        "usage": {},
        "rawResponseText": "",
        "evidence": evidence,
        "evidenceBudgetTokens": evidence_budget,
        "model": args["model"],
    }


def build_fallback_synthesized_working_context(previous: dict[str, Any] | None, reason: str) -> dict[str, Any]:
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


def build_evidence_backed_compaction_synthesis(args: dict[str, Any]) -> dict[str, Any]:
    source_start = int(args.get("sourceStart") or 0)
    source_end = source_start + len(args.get("sourceMessages") or [])
    reason = collapse_whitespace(str(args.get("reason") or "Compaction synthesis returned malformed JSON."))
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
            "recentRequests": _dedupe_strings([*(previous_goals.get("recentRequests") or []), *recent_user_requests])[-48:],
            "constraints": previous_goals.get("constraints") or [],
            "preferences": previous_goals.get("preferences") or [],
        },
        "currentState": {
            "completed": previous_state.get("completed") or [],
            "inProgress": _dedupe_strings([*(previous_state.get("inProgress") or []), "Compacted history is preserved as deterministic trace evidence because synthesis returned malformed JSON."])[-64:],
            "blocked": previous_state.get("blocked") or [],
        },
        "decisions": previous.get("decisions") or [],
        "discoveries": previous.get("discoveries") or [],
        "activity": {
            "commands": previous_activity.get("commands") or [],
            "searches": previous_activity.get("searches") or [],
            "web": previous_activity.get("web") or [],
            "failures": _dedupe_strings([*(previous_activity.get("failures") or []), reason])[-MAX_FAILURES:],
        },
        "nextSteps": previous.get("nextSteps") or ["Use the recent raw messages for the immediate continuation state.", "Use preserved trace as authoritative evidence for compacted history."],
        "criticalContext": _dedupe_strings(
            [
                *(previous.get("criticalContext") or []),
                f"Preserved trace is authoritative for compacted source messages {source_start}-{max(source_end - 1, source_start)}.",
                f"Evidence entries included: {args['evidence']['includedEntryCount']}/{args['evidence']['entryCount']}; evidence tokens: {args['evidence']['tokenEstimate']}/{args['evidence']['tokenBudget']}.",
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
        if not _is_genuine_user_request(message):
            continue
        text = _extract_message_text(message)
        if text:
            requests.append(truncate(text, 600))
    return requests[-12:]


def _dedupe_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _extract_message_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return collapse_whitespace(content) or None
    if isinstance(content, list):
        text = "\n\n".join(str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text")
        return collapse_whitespace(text) or None
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
