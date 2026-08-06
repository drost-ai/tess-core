from __future__ import annotations

import math

import json
import re
from ..._utils import now_iso
from .shared import MAX_ACTIVITY_LINES, MAX_FAILURES, _bound_preserved_trace_record
from .utils import collapseWhitespace
from typing import Any


def _finite_non_negative_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


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
        "todos": {
            "open": asStringArray(todos.get("open")),
            "completed": asStringArray(todos.get("completed")),
            "originalItemCount": _finite_non_negative_number(todos.get("originalItemCount")),
            "returnedItemCount": _finite_non_negative_number(todos.get("returnedItemCount")),
            "omittedItemCount": _finite_non_negative_number(todos.get("omittedItemCount")),
        },
        "activity": {"commands": asStringArray(activity.get("commands")), "searches": asStringArray(activity.get("searches")), "web": asStringArray(activity.get("web")), "failures": asStringArray(activity.get("failures"))},
        "preservedTrace": coercePreservedTrace(record.get("preservedTrace")),
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
            "keyFiles": coerceRelevantFiles(project.get("keyFiles"))[:96],
        },
        "goals": {
            "active": asStringArray(goals.get("active"))[:24],
            "recentRequests": asStringArray(goals.get("recentRequests"))[:48],
            "constraints": asStringArray(goals.get("constraints"))[:48],
            "preferences": asStringArray(goals.get("preferences"))[:48],
        },
        "currentState": {
            "completed": asStringArray(current_state.get("completed"))[:96],
            "inProgress": asStringArray(current_state.get("inProgress"))[:64],
            "blocked": asStringArray(current_state.get("blocked"))[:48],
        },
        "decisions": asStringArray(record.get("decisions"))[:96],
        "discoveries": asStringArray(record.get("discoveries"))[:128],
        "activity": {
            "commands": asStringArray(activity.get("commands"))[:MAX_ACTIVITY_LINES],
            "searches": asStringArray(activity.get("searches"))[:MAX_ACTIVITY_LINES],
            "web": asStringArray(activity.get("web"))[:MAX_ACTIVITY_LINES],
            "failures": asStringArray(activity.get("failures"))[:MAX_FAILURES],
        },
        "nextSteps": asStringArray(record.get("nextSteps"))[:64],
        "criticalContext": asStringArray(record.get("criticalContext"))[:128],
    }


def coercePreservedTrace(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    for entry in reversed(value):
        record = asRecord(entry)
        if not record:
            continue
        source = asRecord(record.get("sourceRange")) or {}
        return [
            _bound_preserved_trace_record({
                "sourceRange": {
                    "generation": source.get("generation") if isinstance(source.get("generation"), int) else 1,
                    "start": source.get("start") if isinstance(source.get("start"), int) else 0,
                    "end": source.get("end") if isinstance(source.get("end"), int) else 0,
                    "messageCount": source.get("messageCount") if isinstance(source.get("messageCount"), int) else 0,
                    "createdAt": source.get("createdAt") if isinstance(source.get("createdAt"), str) else now_iso(),
                },
                "entryCount": record.get("entryCount") if isinstance(record.get("entryCount"), int) else 0,
                "includedEntryCount": record.get("includedEntryCount") if isinstance(record.get("includedEntryCount"), int) else 0,
                "omittedEntryCount": record.get("omittedEntryCount") if isinstance(record.get("omittedEntryCount"), int) else 0,
                "tokenEstimate": record.get("tokenEstimate") if isinstance(record.get("tokenEstimate"), int) else 0,
                "tokenBudget": record.get("tokenBudget") if isinstance(record.get("tokenBudget"), int) else 0,
                "truncated": bool(record.get("truncated")),
                "content": record.get("content") if isinstance(record.get("content"), str) else "",
            })
        ]
    return []


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


def normalizeVerificationVerdict(value: Any) -> str | None:
    return value if value in {"accept", "accept_with_notes", "repair", "reject"} else None


def normalizeVerificationConfidence(value: Any) -> str:
    return value if value in {"low", "medium", "high"} else "medium"


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


def asRecord(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def asStringArray(value: Any) -> list[str]:
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []
