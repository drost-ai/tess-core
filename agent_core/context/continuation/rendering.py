from __future__ import annotations

from .shared import *
from ...todo_policy import TODO_WRITE_LIMITS as _TODO_WRITE_LIMITS
from ...todo_policy import boundTodoViewText as _bound_todo_view_text
from ...todo_policy import selectTodoState as _select_todo_state


def renderContinuationOverlay(session: dict[str, Any]) -> str | None:
    parts = [renderTodoOverlay(session), _render_active_task_overlay(session), renderPlanOverlay(session)]
    return "\n\n".join(part for part in parts if part) or None


def renderTodoOverlay(session: dict[str, Any]) -> str | None:
    selected = _select_todo_state(session.get("todos", []), {"maxOpen": 12, "maxCompleted": 6})
    open_entries = []
    for todo in selected["open"]:
        status = "in progress" if todo.get("status") == "in_progress" else "pending" if todo.get("status") == "pending" else "unknown"
        open_entries.append(f"{status}: {todo.get('activeForm') or todo.get('content')}")
    completed = [todo.get("content") for todo in selected["completed"]]
    if not open_entries and not completed:
        return None
    return "\n".join(
        [
            "<todo_state>",
            f"total={selected['total']}",
            f"returned={len(open_entries) + len(completed)}",
            f"omitted={max(0, selected['total'] - len(open_entries) - len(completed))}",
            *[f"open={entry}" for entry in open_entries],
            *[f"completed={entry}" for entry in completed if entry],
            "</todo_state>",
        ]
    )


def _render_active_task_overlay(session: dict[str, Any]) -> str | None:
    active = (session.get("metadata") or {}).get("activeTask")
    if not isinstance(active, dict) or not str(active.get("content") or "").strip():
        return None
    lines = [
        "<active_task>",
        f"source={active.get('source')}",
        f"content={_bound_todo_view_text(active.get('content'))}",
    ]
    if active.get("activeForm"):
        lines.append(f"active_form={_bound_todo_view_text(active.get('activeForm'), _TODO_WRITE_LIMITS['maxViewActiveFormBytes'])}")
    lines.append("</active_task>")
    return "\n".join(lines)


def renderPlanOverlay(session: dict[str, Any]) -> str | None:
    plan_state = (session.get("metadata") or {}).get("planMode")
    if not isinstance(plan_state, dict) or plan_state.get("state") == "inactive":
        return None
    lines = ["<plan_state>", f"state={plan_state.get('state')}"]
    if plan_state.get("requestId"):
        lines.append(f"request_id={plan_state['requestId']}")
    if plan_state.get("filePath"):
        lines.append(f"file={plan_state['filePath']}")
    if isinstance(plan_state.get("plan"), str) and plan_state["plan"].strip():
        lines.append(f"summary={truncate(plan_state['plan'].strip(), 800)}")
    lines.append("</plan_state>")
    return "\n".join(lines)


def renderPartialCompactionSummary(summary: dict[str, list[str]], reactiveAttempt: int = 0) -> str:
    lines = ["Partial continuation summary for older raw context in this session:"]
    entry_limit = 2 if reactiveAttempt >= 2 else 3 if reactiveAttempt >= 1 else 4
    appendSummarySection(lines, "Carry-forward requests", summary["requests"][:entry_limit])
    appendSummarySection(lines, "Relevant files", summary["files"][: entry_limit + 1])
    appendSummarySection(lines, "Commands and probes", summary["commands"][:entry_limit])
    appendSummarySection(lines, "Assistant findings", summary["findings"][:entry_limit])
    appendSummarySection(lines, "Observed failures", summary["failures"][:entry_limit])
    if len(lines) == 1:
        lines.append("- Older context was compacted, but no structured carry-forward items were extracted.")
    return "\n".join(lines)


def renderContextReliefPrompt(summary: dict[str, Any]) -> str | None:
    if not summary.get("applied") or not summary.get("appliedModes"):
        return None
    if not any(step.get("requiresReinjection") for step in summary.get("steps", []) if isinstance(step, dict)):
        return None
    lines = [
        "<context_reduction_summary>",
        f"pressure_level={summary.get('pressureLevel')}",
        f"reactive_attempt={summary.get('reactiveAttempt')}",
        f"selected_mode={summary.get('selectedMode')}",
    ]
    if summary.get("reinjectionSummary"):
        lines.append(f"reinjection_summary={summary['reinjectionSummary']}")
    lines.extend(f"reinjection={line}" for line in summary.get("reinjectionLines", []))
    lines.append("</context_reduction_summary>")
    return "\n".join(lines)


def summarizeContinuationRegion(messages: list[dict[str, Any]]) -> dict[str, list[str]]:
    requests: list[str] = []
    files: list[str] = []
    commands: list[str] = []
    findings: list[str] = []
    failures: list[str] = []

    def push_line(target: list[str], value: str, limit: int = 220) -> None:
        normalized = truncate(re.sub(r"\s+", " ", value).strip(), limit)
        if normalized and normalized not in target:
            target.append(normalized)

    def push_unique(target: list[str], value: str) -> None:
        if value and value not in target:
            target.append(value)

    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            push_line(requests if message.get("role") == "user" else findings, content, 220)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                push_line(requests if message.get("role") == "user" else findings, str(block.get("text") or ""), 220)
                continue
            if block.get("type") == "tool_use":
                input_value = block.get("input") if isinstance(block.get("input"), dict) else {}
                file_path = extractFilePath(input_value)
                if file_path:
                    push_unique(files, file_path)
                command = extractCommand(input_value)
                if command:
                    push_unique(commands, truncate(command, 200))
                continue
            if block.get("type") == "tool_result":
                content = _tool_result_content_string(block.get("content"))
                parsed = parseStructuredToolResult(content)
                if parsed.get("filePath"):
                    push_unique(files, parsed["filePath"])
                if parsed.get("command"):
                    push_unique(commands, truncate(parsed["command"], 200))
                if parsed.get("failure"):
                    push_unique(failures, parsed["failure"])
                elif block.get("is_error"):
                    push_unique(failures, truncate(re.sub(r"\s+", " ", content), 200))

    return {
        "requests": requests[-4:],
        "files": files[:6],
        "commands": commands[:5],
        "findings": findings[-4:],
        "failures": failures[-4:],
    }


def appendSummarySection(lines: list[str], heading: str, entries: list[str]) -> None:
    if not entries:
        return
    lines.append(f"{heading}:")
    lines.extend(f"- {entry}" for entry in entries)


def renderRegionSummaryReinjectionLines(summary: dict[str, list[str]]) -> list[str]:
    lines: list[str] = []
    if summary["requests"]:
        lines.append(f"recent requests: {' | '.join(summary['requests'])}")
    if summary["files"]:
        lines.append(f"relevant files: {', '.join(summary['files'])}")
    if summary["commands"]:
        lines.append(f"commands and probes: {' | '.join(summary['commands'])}")
    if summary["findings"]:
        lines.append(f"assistant findings: {' | '.join(summary['findings'])}")
    if summary["failures"]:
        lines.append(f"observed failures: {' | '.join(summary['failures'])}")
    return lines


def normalizeReinjectionLines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        normalized = re.sub(r"\s+", " ", str(line)).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(truncate(normalized, 260))
        if len(output) >= 6:
            break
    return output
