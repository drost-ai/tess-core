from __future__ import annotations

from .shared import *


async def buildAgentsReport(session: dict[str, Any], arg_text: str = "") -> str:
    return await _build_agents_report(session, {}, arg_text)


async def _handle_agents(
    arg_text: str,
    session: dict[str, Any],
    config: dict[str, Any],
    hooks: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    related_agents = [record for record in await listChildAgents() if _is_related_child_agent(session, record)]
    normalized = arg_text.strip().lower()
    subcommand, query = _split_subcommand(arg_text)
    if not normalized:
        return _info(hooks, "Agents", await _build_agents_report(session, config, ""))
    if subcommand in {"inspect", "attach", "stop", "retry"}:
        if not query:
            return _error(hooks, "Usage: /agents [inspect|attach|stop|retry <id>]")
        record = _resolve_agent_record(related_agents, query)
        if not record:
            return _error(hooks, f"No child agent matched: {query}")
        if subcommand == "inspect":
            return _info(hooks, "Agent", formatChildAgentRecord(record))
        if subcommand == "attach":
            request_resume = hooks.get("requestResume") if isinstance(hooks, dict) else None
            if not callable(request_resume):
                return _error(hooks, "/agents attach is only available in the full-screen TUI.")
            target_session_id = record.get("attachableSessionId") or record.get("sessionId")
            target_session = await loadSession(target_session_id)
            if not target_session:
                return _error(hooks, f"Attach target not found: {target_session_id}")
            if target_session.get("id") == session.get("id"):
                return _info(hooks, "Agent", f"{record.get('name') or record.get('agentType') or _short_id(record.get('agentId', ''))} belongs to the current session.")
            await _persist(hooks)
            request_resume(target_session)
            return _handled(f"resuming {target_session.get('id')}")
        if not record.get("taskId"):
            return _error(hooks, f"Child agent {_short_id(record.get('agentId', ''))} is not linked to a supervised task.")
        supervisor, should_close = await _resolve_task_supervisor(args)
        if not supervisor:
            return _error(hooks, "This child-agent action requires the local task supervisor daemon.")
        try:
            if subcommand == "stop":
                stop_fn = supervisor.get("stopTask") if isinstance(supervisor, dict) else None
                if not callable(stop_fn):
                    return _error(hooks, "Task supervisor does not support stopTask.")
                stopped = await _maybe_await(stop_fn(record["taskId"]))
                if not stopped:
                    return _error(hooks, f"Child agent could not be stopped: {record['taskId']}")
                return _info(hooks, "Agent", f"Stopped {record.get('name') or record.get('agentType') or _short_id(record.get('agentId', ''))}")
            retry_fn = supervisor.get("retryTask") if isinstance(supervisor, dict) else None
            if not callable(retry_fn):
                return _error(hooks, "Task supervisor does not support retryTask.")
            try:
                retried = await _maybe_await(retry_fn(record["taskId"]))
            except Exception as exc:
                return _error(hooks, str(exc))
            return _info(
                hooks,
                "Agent",
                "\n".join(
                    [
                        f"Retried {record.get('name') or record.get('agentType') or _short_id(record.get('agentId', ''))}",
                        f"new task     {(retried or {}).get('taskId') or (retried or {}).get('id')}",
                        *([f"output       {retried.get('outputFile')}"] if isinstance(retried, dict) and retried.get("outputFile") else []),
                    ]
                ),
            )
        finally:
            await _close_task_supervisor_if_needed(supervisor, should_close)
    return _info(hooks, "Agents", await _build_agents_report(session, config, arg_text))


async def _build_agents_report(session: dict[str, Any], config: dict[str, Any], arg_text: str = "") -> str:
    related_agents = [record for record in await listChildAgents() if _is_related_child_agent(session, record)]
    normalized = arg_text.strip().lower()
    if not normalized:
        configured = config.get("agents") or {}
        active = [record for record in related_agents if _is_active_child_agent(record)]
        awaiting = [record for record in related_agents if record.get("status") == "awaiting_plan_approval"]
        configured_lines = _format_configured_agents(configured)
        return "\n".join(
            [
                configured_lines if configured_lines else "No configured agents.",
                "",
                f"child runs   {len(related_agents)}",
                f"active       {len(active)}",
                f"awaiting     {len(awaiting)}",
                "tip          /agents running or /agents all",
            ]
        )
    if normalized in {"running", "active"}:
        visible = [record for record in related_agents if _is_active_child_agent(record)]
    elif normalized in {"all", "history"}:
        visible = related_agents
    else:
        visible = [record for record in related_agents if _matches_agent_query(record, normalized)]
    if not visible:
        if normalized in {"running", "active"}:
            return "No active child agents for this session."
        return f"No child agents matched: {arg_text}"
    return "\n\n".join(formatChildAgentRecord(record) for record in visible[:12])


def _format_configured_agents(agents: dict[str, Any]) -> str:
    lines = []
    for name, agent in agents.items():
        description = agent.get("description") if isinstance(agent, dict) else getattr(agent, "description", "")
        lines.append(f"{str(name).ljust(12)} {description or ''}".rstrip())
    return "\n".join(lines)


def _resolve_agent_record(records: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    for record in records:
        if normalized in _compact_tokens(record.get("agentId"), record.get("sessionId")):
            return record
    for record in records:
        values = _compact_tokens(record.get("agentId"), record.get("sessionId"), record.get("taskId"))
        if any(normalized in value for value in values):
            return record
    return None


def _matches_agent_query(record: dict[str, Any], query: str) -> bool:
    return any(
        query in value
        for value in _compact_tokens(
            record.get("agentId"),
            record.get("sessionId"),
            record.get("parentSessionId"),
            record.get("name"),
            record.get("agentType"),
            record.get("teamName"),
            record.get("status"),
            record.get("taskId"),
        )
    )


def formatChildAgentRecord(record: dict[str, Any]) -> str:
    label = record.get("name") or record.get("agentType") or record.get("agentId")
    mode = "bg" if record.get("runInBackground") else "fg"
    worktree = " worktree" if record.get("isolation") == "worktree" else ""
    detail = [
        str(record.get("status")),
        f"{mode}{worktree}",
        compactModelName(str(record.get("model") or "")),
        str(record.get("startedAt") or "")[:19].replace("T", " "),
    ]
    lines = [
        f"{_short_id(record.get('agentId', ''))}  {label}",
        f"status       {'  '.join(detail)}",
    ]
    for label_name, key in [
        ("agent type", "agentType"),
        ("team", "teamName"),
        ("task", "taskId"),
        ("owner", "owner"),
        ("session", "sessionId"),
        ("attach", "attachableSessionId"),
        ("parent task", "parentTaskId"),
        ("worktree", "worktreePath"),
    ]:
        value = record.get(key)
        if value:
            if key in {"taskId", "sessionId", "attachableSessionId", "parentTaskId"}:
                value = _short_id(str(value))
            lines.append(f"{label_name:<12} {value}")
    if record.get("completedAt"):
        lines.append(f"completed    {str(record.get('completedAt'))[:19].replace('T', ' ')}")
    for label_name, key, limit in [
        ("waiting", "waitingReason", 180),
        ("completion", "completionReason", 180),
        ("result", "resultSummary", 180),
        ("recovery", "recoveryState", 180),
        ("stop reason", "stopReason", 180),
        ("error", "error", 180),
    ]:
        value = record.get(key)
        if value:
            lines.append(f"{label_name:<12} {truncate(collapseWhitespace(value), limit)}")
    if record.get("retryable") is not None or record.get("stoppable") is not None:
        lines.append(
            f"controls     retry {'yes' if record.get('retryable') else 'no'} - stop {'yes' if record.get('stoppable') else 'no'}"
        )
    return "\n".join(lines)


def _is_related_child_agent(session: dict[str, Any], record: dict[str, Any]) -> bool:
    if record.get("parentSessionId") == session.get("id") or record.get("sessionId") == session.get("id"):
        return True
    return record.get("agentId") in (session.get("metadata", {}).get("childAgentIds") or [])


def _is_active_child_agent(record: dict[str, Any]) -> bool:
    return record.get("status") in {"queued", "running", "backgrounded", "awaiting_plan_approval", "awaiting_input"}
