from __future__ import annotations

from ..coerce import as_record, as_string_array

from ...session.transcript import parse_maybe_json

import math

import json
import re
from ..._utils import now_iso
from .shared import MAX_ACTIVITY_LINES, MAX_FAILURES, _bound_preserved_trace_record
from .utils import collapse_whitespace
from typing import Any


def _finite_non_negative_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def parse_compaction_synthesis_response(value: str) -> dict[str, Any] | None:
    candidate = extract_json_object_candidate(value)
    if not candidate:
        return None
    parsed = parse_maybe_json(candidate)
    record = as_record(parsed)
    if not record:
        return None
    quality = as_record(record.get("quality")) or {}
    classification = quality.get("classification") if quality.get("classification") in {"thin", "usable", "rich"} else "usable"
    reason = collapse_whitespace(quality.get("reason")) if isinstance(quality.get("reason"), str) else "Model-backed checkpoint synthesis completed."
    working_context = coerce_synthesized_working_context_fields(record.get("workingContext"))
    if not working_context:
        return None
    return {
        "shouldCheckpoint": record.get("shouldCheckpoint") is not False,
        "qualityClassification": classification,
        "qualityReason": reason,
        "workingContext": working_context,
    }


def parse_compaction_verification_response(value: str) -> dict[str, Any] | None:
    candidate = extract_json_object_candidate(value)
    if not candidate:
        return None
    parsed = parse_maybe_json(candidate)
    record = as_record(parsed)
    if not record:
        return None
    verdict = normalize_verification_verdict(record.get("verdict"))
    if not verdict:
        return None
    confidence = normalize_verification_confidence(record.get("confidence"))
    reason = collapse_whitespace(record.get("reason")) if isinstance(record.get("reason"), str) else "Checkpoint verification completed."
    issues = as_string_array(record.get("issues"))[:12]
    notes = as_string_array(record.get("notes"))[:12]
    repaired = coerce_repaired_checkpoint(record.get("repairedCheckpoint")) if verdict == "repair" else None
    if verdict == "repair" and not repaired:
        return None
    return {"verdict": verdict, "confidence": confidence, "reason": reason, "issues": issues, "notes": notes, "repairedCheckpoint": repaired}


def coerce_working_context_document(value: Any) -> dict[str, Any] | None:
    record = as_record(value)
    if not record or record.get("version") != "v2" or not isinstance(record.get("sessionId"), str):
        return None
    session = as_record(record.get("session"))
    if not session or not isinstance(session.get("cwd"), str) or not isinstance(session.get("model"), str):
        return None
    project = as_record(record.get("project")) or {}
    goals = as_record(record.get("goals")) or {}
    current_state = as_record(record.get("currentState")) or {}
    todos = as_record(record.get("todos")) or {}
    activity = as_record(record.get("activity")) or {}
    compaction = as_record(record.get("compaction"))
    compaction_record = None
    if compaction and compaction.get("provider") == "openai" and isinstance(compaction.get("model"), str):
        compaction_record = {"provider": "openai", "model": compaction["model"]}
        if isinstance(compaction.get("effort"), str):
            compaction_record["effort"] = compaction["effort"]
    source_ranges = []
    if isinstance(record.get("sourceRanges"), list):
        for entry in record["sourceRanges"]:
            source = as_record(entry)
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
        "project": {"description": project.get("description") if isinstance(project.get("description"), str) else None, "facts": coerce_project_facts(project.get("facts")), "keyFiles": coerce_relevant_files(project.get("keyFiles"))},
        "goals": {"active": as_string_array(goals.get("active")), "recentRequests": as_string_array(goals.get("recentRequests")), "constraints": as_string_array(goals.get("constraints")), "preferences": as_string_array(goals.get("preferences"))},
        "currentState": {"completed": as_string_array(current_state.get("completed")), "inProgress": as_string_array(current_state.get("inProgress")), "blocked": as_string_array(current_state.get("blocked"))},
        "decisions": as_string_array(record.get("decisions")),
        "discoveries": as_string_array(record.get("discoveries")),
        "todos": {
            "open": as_string_array(todos.get("open")),
            "completed": as_string_array(todos.get("completed")),
            "originalItemCount": _finite_non_negative_number(todos.get("originalItemCount")),
            "returnedItemCount": _finite_non_negative_number(todos.get("returnedItemCount")),
            "omittedItemCount": _finite_non_negative_number(todos.get("omittedItemCount")),
        },
        "activity": {"commands": as_string_array(activity.get("commands")), "searches": as_string_array(activity.get("searches")), "web": as_string_array(activity.get("web")), "failures": as_string_array(activity.get("failures"))},
        "preservedTrace": coerce_preserved_trace(record.get("preservedTrace")),
        "nextSteps": as_string_array(record.get("nextSteps")),
        "criticalContext": as_string_array(record.get("criticalContext")),
    }


def coerce_synthesized_working_context_fields(value: Any) -> dict[str, Any] | None:
    record = as_record(value)
    if not record:
        return None
    project = as_record(record.get("project")) or {}
    goals = as_record(record.get("goals")) or {}
    current_state = as_record(record.get("currentState")) or {}
    activity = as_record(record.get("activity")) or {}
    return {
        "project": {
            "description": collapse_whitespace(project["description"]) if isinstance(project.get("description"), str) else None,
            "facts": coerce_project_facts(project.get("facts")),
            "keyFiles": coerce_relevant_files(project.get("keyFiles"))[:96],
        },
        "goals": {
            "active": as_string_array(goals.get("active"))[:24],
            "recentRequests": as_string_array(goals.get("recentRequests"))[:48],
            "constraints": as_string_array(goals.get("constraints"))[:48],
            "preferences": as_string_array(goals.get("preferences"))[:48],
        },
        "currentState": {
            "completed": as_string_array(current_state.get("completed"))[:96],
            "inProgress": as_string_array(current_state.get("inProgress"))[:64],
            "blocked": as_string_array(current_state.get("blocked"))[:48],
        },
        "decisions": as_string_array(record.get("decisions"))[:96],
        "discoveries": as_string_array(record.get("discoveries"))[:128],
        "activity": {
            "commands": as_string_array(activity.get("commands"))[:MAX_ACTIVITY_LINES],
            "searches": as_string_array(activity.get("searches"))[:MAX_ACTIVITY_LINES],
            "web": as_string_array(activity.get("web"))[:MAX_ACTIVITY_LINES],
            "failures": as_string_array(activity.get("failures"))[:MAX_FAILURES],
        },
        "nextSteps": as_string_array(record.get("nextSteps"))[:64],
        "criticalContext": as_string_array(record.get("criticalContext"))[:128],
    }


def coerce_preserved_trace(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    for entry in reversed(value):
        record = as_record(entry)
        if not record:
            continue
        source = as_record(record.get("sourceRange")) or {}
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


def coerce_repaired_checkpoint(value: Any) -> dict[str, Any] | None:
    record = as_record(value)
    if not record:
        return None
    quality = as_record(record.get("quality")) or {}
    classification = quality.get("classification") if quality.get("classification") in {"thin", "usable", "rich"} else "usable"
    reason = collapse_whitespace(quality.get("reason")) if isinstance(quality.get("reason"), str) else "Checkpoint repaired by verifier."
    working_context = coerce_synthesized_working_context_fields(record.get("workingContext"))
    if not working_context:
        return None
    return {"qualityClassification": classification, "qualityReason": reason, "workingContext": working_context}


def coerce_project_facts(value: Any) -> dict[str, Any] | None:
    record = as_record(value)
    if not record:
        return None
    languages = as_string_array(record.get("languages"))
    evidence = as_string_array(record.get("evidence"))
    if not any([isinstance(record.get("primaryLanguage"), str), isinstance(record.get("ecosystem"), str), isinstance(record.get("packageManager"), str), languages, evidence]):
        return None
    return {
        "primaryLanguage": record.get("primaryLanguage") if isinstance(record.get("primaryLanguage"), str) else None,
        "languages": languages,
        "ecosystem": record.get("ecosystem") if isinstance(record.get("ecosystem"), str) else None,
        "packageManager": record.get("packageManager") if isinstance(record.get("packageManager"), str) else None,
        "evidence": evidence,
    }


def coerce_relevant_files(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    files = []
    for entry in value:
        record = as_record(entry)
        if record and isinstance(record.get("path"), str):
            files.append({"path": str(record["path"]), "role": "modified" if record.get("role") == "modified" else "read", "note": record.get("note") if isinstance(record.get("note"), str) else None})
    return files


def normalize_verification_verdict(value: Any) -> str | None:
    return value if value in {"accept", "accept_with_notes", "repair", "reject"} else None


def normalize_verification_confidence(value: Any) -> str:
    return value if value in {"low", "medium", "high"} else "medium"




def extract_json_object_candidate(value: str) -> str | None:
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




