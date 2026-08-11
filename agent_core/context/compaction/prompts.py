from __future__ import annotations

import json
from .shared import MAX_TODOS
from .utils import summarize_todo
from typing import Any
from ...payloads.todo_policy import TODO_WRITE_LIMITS as _TODO_WRITE_LIMITS
from ...payloads.todo_policy import bound_todo_view_text as _bound_todo_view_text
from ...payloads.todo_policy import select_todo_state as _select_todo_state


def build_compaction_synthesis_prompt(args: dict[str, Any]) -> str:
    session = args["session"]
    evidence = args["evidence"]
    model = args["model"]
    selected_todos = _select_todo_state(
        session.get("todos", []), {"maxOpen": MAX_TODOS, "maxCompleted": MAX_TODOS}
    )
    active_task = (session.get("metadata") or {}).get("activeTask")
    todo_snapshot = {
        "originalItemCount": selected_todos["total"],
        "returnedItemCount": len(selected_todos["open"]) + len(selected_todos["completed"]),
        "omittedItemCount": max(0, selected_todos["total"] - len(selected_todos["open"]) - len(selected_todos["completed"])),
        "statusCounts": selected_todos["statusCounts"],
        "activeTask": {
            **active_task,
            "content": _bound_todo_view_text(active_task.get("content")),
            "activeForm": _bound_todo_view_text(active_task.get("activeForm"), _TODO_WRITE_LIMITS["maxViewActiveFormBytes"]),
        } if isinstance(active_task, dict) else None,
        "open": [summarize_todo(todo) for todo in selected_todos["open"]],
        "completed": [summarize_todo(todo) for todo in selected_todos["completed"]],
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
        "Compaction preservation policy:",
        f"- Source token estimate: {args.get('sourceTokenEstimate')}",
        f"- Compaction output budget: {args.get('outputTokenBudget')}",
        f"- Target preserved checkpoint tokens: about {args.get('targetPreservationTokens')} (10% of compacted source)",
        "- Use the available budget to preserve trace fidelity. Do not produce a tiny executive summary when the source range is large.",
        "- If you cannot preserve the important trace details within the output budget, set shouldCheckpoint to false and explain the missing fidelity.",
        "",
        "Bounded current todo snapshot (counts describe the full stored state):",
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
        "- Agentic tool traces often contain the only durable reasoning path. Preserve useful failed attempts with their failure reason, exact commands/payloads, target identifiers, artifact paths, current hypotheses, and the next concrete action when they remain relevant.",
        "- Prefer current objectives over superseded earlier asks.",
        "- Treat the todo snapshot, active task, and latest saved plan state as authoritative.",
        "- Return JSON only.",
    ]
    if args.get("previousError"):
        lines.extend(["", "Previous output error:", args["previousError"]])
    return "\n".join(lines)


def build_compaction_verification_prompt(args: dict[str, Any]) -> str:
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
        "Compaction preservation policy:",
        f"- Source token estimate: {args.get('sourceTokenEstimate')}",
        f"- Compaction output budget: {args.get('outputTokenBudget')}",
        f"- Target preserved checkpoint tokens: about {args.get('targetPreservationTokens')} (10% of compacted source)",
        "- Reject or repair checkpoints that over-compress the source into a tiny summary.",
        "- A valid checkpoint should use enough of the available budget to preserve goals, decisions, evidence, commands, outputs, failures, artifacts, and chronology needed for continuation.",
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
        "- Reject or repair checkpoints that flatten an active agentic trace into a vague summary without the current path, material failed attempts, exact commands/payloads/artifact paths, and next concrete step.",
        "- Reject or repair checkpoints that omit important trace evidence merely to be concise.",
        "- If you repair, return a fully repaired checkpoint in repairedCheckpoint.",
        "- If you cannot provide a fully repaired checkpoint, return reject instead of repair.",
        "- Return JSON only.",
    ]
    if args.get("previousError"):
        lines.extend(["", "Previous output error:", args["previousError"]])
    return "\n".join(lines)


def render_working_context_prompt_context(
    context: dict[str, Any],
) -> str:
    return _render_full_working_context_prompt_context(context)


def _render_full_working_context_prompt_context(context: dict[str, Any]) -> str:
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
    append_prompt_list(lines, "active_goals", goals.get("active") or [])
    append_prompt_list(lines, "recent_requests", goals.get("recentRequests") or [])
    append_prompt_list(lines, "constraints", goals.get("constraints") or [])
    append_prompt_list(lines, "preferences", goals.get("preferences") or [])
    append_prompt_list(lines, "completed", current_state.get("completed") or [])
    append_prompt_list(lines, "in_progress", current_state.get("inProgress") or [])
    append_prompt_list(lines, "blocked", current_state.get("blocked") or [])
    append_prompt_list(lines, "decisions", context.get("decisions") or [])
    append_prompt_list(lines, "discoveries", context.get("discoveries") or [])
    append_prompt_list(lines, "relevant_files", [_format_relevant_file_prompt(record) for record in project.get("keyFiles", [])])
    append_prompt_list(lines, "project_evidence", facts.get("evidence") or [])
    append_prompt_list(lines, "open_todos", todos.get("open") or [])
    if todos.get("originalItemCount") is not None:
        lines.append(
            f"todo_cardinality: total={todos['originalItemCount']} "
            f"returned={todos.get('returnedItemCount', 0)} omitted={todos.get('omittedItemCount', 0)}"
        )
    append_prompt_list(lines, "recent_searches", activity.get("searches") or [])
    append_prompt_list(lines, "recent_commands", activity.get("commands") or [])
    append_prompt_list(lines, "recent_web", activity.get("web") or [])
    append_prompt_list(lines, "recent_failures", activity.get("failures") or [])
    append_prompt_list(lines, "next_steps", context.get("nextSteps") or [])
    append_prompt_list(lines, "critical_context", context.get("criticalContext") or [])
    append_preserved_trace(lines, context)
    lines.append("</working_context>")
    return "\n".join(lines)


def append_preserved_trace(lines: list[str], context: dict[str, Any]) -> None:
    traces = context.get("preservedTrace") or []
    if not isinstance(traces, list) or not traces:
        return
    lines.append("preserved_trace:")
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        source_range = trace.get("sourceRange") or {}
        lines.append(
            f"<trace source_start=\"{source_range.get('start')}\" source_end=\"{source_range.get('end')}\" "
            f"messages=\"{source_range.get('messageCount')}\" tokens=\"{trace.get('tokenEstimate')}\" "
            f"included_entries=\"{trace.get('includedEntryCount')}\" omitted_entries=\"{trace.get('omittedEntryCount')}\" "
            f"truncated=\"{str(bool(trace.get('truncated'))).lower()}\">"
        )
        lines.append(str(trace.get("content") or ""))
        lines.append("</trace>")


def append_project_facts_section(lines: list[str], context: dict[str, Any]) -> None:
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


def append_string_section(lines: list[str], heading: str, entries: list[str]) -> None:
    values = [entry for entry in entries if entry]
    if not values:
        return
    lines.extend(["", f"## {heading}", ""])
    lines.extend(f"- {entry}" for entry in values)


def append_subsection(lines: list[str], heading: str, entries: list[str]) -> None:
    lines.append(f"### {heading}")
    if not entries:
        lines.extend(["- None.", ""])
        return
    lines.extend(f"- {entry}" for entry in entries)
    lines.append("")


def append_relevant_files_section(lines: list[str], files: list[dict[str, Any]]) -> None:
    if not files:
        return
    lines.extend(["", "## Relevant Files", ""])
    for file_record in files:
        note = f" — {file_record['note']}" if file_record.get("note") else ""
        lines.append(f"- `{file_record.get('path')}`{note}")


def append_prompt_list(lines: list[str], heading: str, entries: list[str]) -> None:
    values = [entry for entry in entries if entry]
    if not values:
        return
    lines.append(f"{heading}:")
    lines.extend(f"- {entry}" for entry in values)


def append_activity_section(lines: list[str], heading: str, entries: list[str]) -> None:
    if not entries:
        return
    lines.extend([f"## {heading}", ""])
    lines.extend(entries)
    lines.append("")


def _format_relevant_file_prompt(record: dict[str, Any]) -> str:
    note = f" ({record.get('note')})" if record.get("note") else ""
    return f"{record.get('path')}{note}"
