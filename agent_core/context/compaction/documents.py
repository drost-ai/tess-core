from __future__ import annotations

from .artifacts import _read_text
from .models import _model_record
from .parsing import (
    as_record,
    as_string_array,
    coerce_relevant_files,
    coerce_working_context_document,
    parseMaybeJson,
)
from .prompts import (
    append_activity_section,
    append_project_facts_section,
    append_relevant_files_section,
    append_string_section,
    append_subsection,
)
from .shared import (
    MAX_ACTIVITY_LINES,
    MAX_FAILURES,
    MAX_TODOS,
    _bound_preserved_trace_record,
)
from .utils import summarize_todo, truncate
from typing import Any
from ...payloads.todo_policy import selectTodoState as _select_todo_state


def build_compaction_quality_from_synthesis(synthesized: dict[str, Any]) -> dict[str, Any]:
    return build_compaction_quality_from_candidate(
        {
            "qualityClassification": synthesized.get("qualityClassification"),
            "qualityReason": synthesized.get("qualityReason"),
            "workingContext": synthesized.get("workingContext"),
        }
    )


def build_compaction_quality_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    classification = candidate.get("qualityClassification") if candidate.get("qualityClassification") in {"thin", "usable", "rich"} else "usable"
    score = 16 if classification == "rich" else 8 if classification == "usable" else 3
    working_context = candidate.get("workingContext") or {}
    goals = as_record(working_context.get("goals")) or {}
    project = as_record(working_context.get("project")) or {}
    activity = as_record(working_context.get("activity")) or {}
    return {
        "passed": classification != "thin",
        "score": score,
        "classification": classification,
        "detail": candidate.get("qualityReason") or "Model-backed checkpoint synthesis completed.",
        "metrics": {
            "userRequests": len(as_string_array(goals.get("active"))),
            "relevantFiles": len(coerce_relevant_files(project.get("keyFiles"))),
            "decisions": len(as_string_array(working_context.get("decisions"))),
            "discoveries": len(as_string_array(working_context.get("discoveries"))),
            "reads": 0,
            "fileChanges": 0,
            "commands": len(as_string_array(activity.get("commands"))),
            "searches": len(as_string_array(activity.get("searches"))),
            "web": len(as_string_array(activity.get("web"))),
            "failures": len(as_string_array(activity.get("failures"))),
        },
    }


def build_synthesized_working_context_document(args: dict[str, Any]) -> dict[str, Any]:
    return build_working_context_document_from_candidate(
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
            "preservedTraceEvidence": args["synthesized"].get("evidence"),
        }
    )


def build_working_context_document_from_candidate(args: dict[str, Any]) -> dict[str, Any]:
    previous = args.get("previous") or {}
    session = args["session"]
    candidate = args["candidate"]
    working_context = candidate["workingContext"]
    model = candidate["model"]
    selected_todos = _select_todo_state(
        session.get("todos", []), {"maxOpen": MAX_TODOS, "maxCompleted": MAX_TODOS}
    )
    open_todos = [summarize_todo(todo) for todo in selected_todos["open"]]
    completed_todos = [summarize_todo(todo) for todo in selected_todos["completed"]]
    preserved_trace = _latest_preserved_trace(
        previous.get("preservedTrace"),
        args.get("sourceRange"),
        args.get("preservedTraceEvidence"),
    )
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
        "todos": {
            "open": open_todos,
            "completed": completed_todos,
            "originalItemCount": selected_todos["total"],
            "returnedItemCount": len(open_todos) + len(completed_todos),
            "omittedItemCount": max(0, selected_todos["total"] - len(open_todos) - len(completed_todos)),
        },
        "activity": working_context["activity"],
        "preservedTrace": preserved_trace,
        "nextSteps": working_context["nextSteps"],
        "criticalContext": working_context["criticalContext"],
    }


def build_preserved_trace_record(sourceRange: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    return _bound_preserved_trace_record({
        "sourceRange": sourceRange,
        "entryCount": evidence.get("entryCount"),
        "includedEntryCount": evidence.get("includedEntryCount"),
        "omittedEntryCount": evidence.get("omittedEntryCount"),
        "tokenEstimate": evidence.get("tokenEstimate"),
        "tokenBudget": evidence.get("tokenBudget"),
        "truncated": bool(evidence.get("truncated")),
        "content": str(evidence.get("transcript") or ""),
    })


def _latest_preserved_trace(
    previous_traces: Any,
    source_range: dict[str, Any] | None,
    evidence: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if isinstance(evidence, dict) and isinstance(source_range, dict):
        return [build_preserved_trace_record(source_range, evidence)]
    if not isinstance(previous_traces, list):
        return []
    for trace in reversed(previous_traces):
        if isinstance(trace, dict):
            return [_bound_preserved_trace_record(trace)]
    return []


def build_model_backed_compaction_state_snapshot(args: dict[str, Any]) -> dict[str, Any]:
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


def render_summary_markdown(context: dict[str, Any], meta: dict[str, Any]) -> str:
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
    append_project_facts_section(lines, context)
    append_string_section(lines, "User Goal", context["goals"]["active"])
    append_string_section(lines, "Constraints And Preferences", [*context["goals"]["constraints"], *context["goals"]["preferences"]])
    append_string_section(lines, "Discoveries", context["discoveries"])
    append_relevant_files_section(lines, context["project"].get("keyFiles") or [])
    lines.extend(["", "## Current State", ""])
    append_subsection(lines, "Completed", context["currentState"]["completed"])
    append_subsection(lines, "In Progress", context["currentState"]["inProgress"])
    append_subsection(lines, "Blocked", context["currentState"]["blocked"])
    append_string_section(lines, "Key Decisions", context["decisions"])
    append_string_section(lines, "Verification Notes", [*verification.get("notes", []), *[f"Issue: {entry}" for entry in verification.get("issues", [])]])
    activity = context["activity"]
    append_string_section(
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
    cardinality = (
        [f"{todos['originalItemCount']} stored; {todos.get('returnedItemCount', 0)} shown; {todos.get('omittedItemCount', 0)} omitted"]
        if todos.get("originalItemCount") is not None
        else []
    )
    append_string_section(lines, "Todo State", [*cardinality, *[f"Open: {entry}" for entry in todos.get("open", [])], *[f"Completed: {entry}" for entry in todos.get("completed", [])]])
    append_string_section(lines, "Next Steps", context["nextSteps"])
    append_string_section(lines, "Critical Context", context["criticalContext"])
    lines.extend(["", "## Resume Guidance", "", "- Use this checkpoint as the authoritative source for earlier compacted work.", "- Prefer the recent raw conversation turns when they conflict with this checkpoint.", ""])
    return "\n".join(lines)


def render_decisions_markdown(context: dict[str, Any]) -> str:
    lines = ["# Assistant Decisions", ""]
    for decision in context.get("decisions", [])[:12]:
        lines.append(f"- {decision}")
    if len(lines) == 2:
        lines.append("- No durable decisions extracted yet.")
    lines.append("")
    return "\n".join(lines)


def render_activity_markdown(context: dict[str, Any]) -> str:
    lines = ["# Assistant Activity Ledger", ""]
    relevant_files = []
    for record in (context.get("project", {}).get("keyFiles") or [])[:MAX_ACTIVITY_LINES]:
        note = f" ({record.get('note')})" if record.get("note") else ""
        role = "modified" if record.get("role") == "modified" else "read"
        relevant_files.append(f"- {role} `{record.get('path')}`{note}")
    append_activity_section(
        lines,
        "Relevant Files",
        relevant_files,
    )
    activity = context.get("activity") or {}
    append_activity_section(lines, "Commands", [f"- {entry}" for entry in activity.get("commands", [])[:MAX_ACTIVITY_LINES]])
    append_activity_section(lines, "Searches", [f"- {entry}" for entry in activity.get("searches", [])[:MAX_ACTIVITY_LINES]])
    append_activity_section(lines, "Web", [f"- {entry}" for entry in activity.get("web", [])[:MAX_ACTIVITY_LINES]])
    append_activity_section(lines, "Failures", [f"- {entry}" for entry in activity.get("failures", [])[:MAX_FAILURES]])
    return "\n".join(lines) + "\n"


async def load_working_context_artifact(filePath: str | None) -> dict[str, Any] | None:
    if not filePath:
        return None
    raw = _read_text(filePath)
    if not raw.strip():
        return None
    return coerce_working_context_document(parseMaybeJson(raw))


def assert_model_backed_compaction_artifacts(stateSnapshot: dict[str, Any], workingContext: dict[str, Any]) -> None:
    if not as_record(stateSnapshot.get("compactionModel")) or not as_record(stateSnapshot.get("synthesis")):
        raise RuntimeError("Compaction state snapshot is not model-backed; refusing to persist checkpoint.")
    if not as_record(stateSnapshot.get("verificationModel")) or not as_record(stateSnapshot.get("verification")):
        raise RuntimeError("Compaction state snapshot is not verifier-backed; refusing to persist checkpoint.")
    if not workingContext.get("compaction"):
        raise RuntimeError("Working context is missing compaction model metadata; refusing to persist checkpoint.")
