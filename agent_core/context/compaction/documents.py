from __future__ import annotations

from .shared import *
from ...todo_policy import selectTodoState as _select_todo_state


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
            "preservedTraceEvidence": args["synthesized"].get("evidence"),
        }
    )


def buildWorkingContextDocumentFromCandidate(args: dict[str, Any]) -> dict[str, Any]:
    previous = args.get("previous") or {}
    session = args["session"]
    candidate = args["candidate"]
    working_context = candidate["workingContext"]
    model = candidate["model"]
    selected_todos = _select_todo_state(
        session.get("todos", []), {"maxOpen": MAX_TODOS, "maxCompleted": MAX_TODOS}
    )
    open_todos = [summarizeTodo(todo) for todo in selected_todos["open"]]
    completed_todos = [summarizeTodo(todo) for todo in selected_todos["completed"]]
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
        "todos": {
            "open": open_todos,
            "completed": completed_todos,
            "originalItemCount": selected_todos["total"],
            "returnedItemCount": len(open_todos) + len(completed_todos),
            "omittedItemCount": max(0, selected_todos["total"] - len(open_todos) - len(completed_todos)),
        },
        "activity": working_context["activity"],
        "preservedTrace": [
            *(previous.get("preservedTrace") or []),
            *([buildPreservedTraceRecord(args["sourceRange"], args["preservedTraceEvidence"])] if args.get("preservedTraceEvidence") else []),
        ][-12:],
        "nextSteps": working_context["nextSteps"],
        "criticalContext": working_context["criticalContext"],
    }


def buildPreservedTraceRecord(sourceRange: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "sourceRange": sourceRange,
        "entryCount": evidence.get("entryCount"),
        "includedEntryCount": evidence.get("includedEntryCount"),
        "omittedEntryCount": evidence.get("omittedEntryCount"),
        "tokenEstimate": evidence.get("tokenEstimate"),
        "tokenBudget": evidence.get("tokenBudget"),
        "truncated": evidence.get("truncated"),
        "content": evidence.get("transcript"),
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
    cardinality = (
        [f"{todos['originalItemCount']} stored; {todos.get('returnedItemCount', 0)} shown; {todos.get('omittedItemCount', 0)} omitted"]
        if todos.get("originalItemCount") is not None
        else []
    )
    appendStringSection(lines, "Todo State", [*cardinality, *[f"Open: {entry}" for entry in todos.get("open", [])], *[f"Completed: {entry}" for entry in todos.get("completed", [])]])
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


async def loadWorkingContextArtifact(filePath: str | None) -> dict[str, Any] | None:
    if not filePath:
        return None
    raw = _read_text(filePath)
    if not raw.strip():
        return None
    return coerceWorkingContextDocument(parseMaybeJson(raw))


def assertModelBackedCompactionArtifacts(stateSnapshot: dict[str, Any], workingContext: dict[str, Any]) -> None:
    if not asRecord(stateSnapshot.get("compactionModel")) or not asRecord(stateSnapshot.get("synthesis")):
        raise RuntimeError("Compaction state snapshot is not model-backed; refusing to persist checkpoint.")
    if not asRecord(stateSnapshot.get("verificationModel")) or not asRecord(stateSnapshot.get("verification")):
        raise RuntimeError("Compaction state snapshot is not verifier-backed; refusing to persist checkpoint.")
    if not workingContext.get("compaction"):
        raise RuntimeError("Working context is missing compaction model metadata; refusing to persist checkpoint.")
