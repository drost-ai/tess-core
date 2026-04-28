from __future__ import annotations

import os
from typing import Any

from ._utils import now_iso
from .compaction import runCompaction
from .config import (
    ensureProviderForModelSelection,
    getDefaultEffortForProvider,
    getDefaultModelForProvider,
    listModelsForProvider,
    listProviderProfiles,
    listProviders,
    modelSupportsEffort,
    normalizeEffortForModel,
    normalizeEffortLevel,
    normalizeModelName,
    normalizeProviderName,
    resolveProviderForModelSelection,
    normalizeProviderProfileName,
    resolveFallbackProviderProfileName,
)
from .debug_bundle import createDebugBundle
from .durable_memory import synchronizeManualProjectMemory
from .daemon import connectTaskSupervisorDaemonClient, ensureTaskSupervisorDaemon, stopTaskSupervisorDaemon
from .platform import collectDoctorReport
from .remote import formatBridgeTail, formatRemoteSessionCandidate, sendBridgeMessage, updateRemoteSession
from .storage import (
    compactionCheckpointPath,
    compactionManifestPath,
    getProjectMemoryPath,
    getDefaultAutoMode,
    listBridges,
    listChildAgents,
    listPlanApprovals,
    listRemoteSessions,
    listSessionMonitors,
    listSessionTaskRecords,
    listSessions,
    loadAutoMode,
    loadBridge,
    loadCompactionManifest,
    loadDaemonState,
    loadRemoteSession,
    loadSession,
    loadSettings,
    loadTaskRecord,
    saveBridge,
    saveAutoMode,
    saveSettings,
    saveMonitor,
)
from .transcript import buildTranscriptSnapshot


AVAILABLE_TUI_THEMES = ["tess", "copper", "ocean", "forest"]
PERMISSION_MODE_ORDER = ["acceptEdits", "bypassPermissions", "default", "dontAsk", "plan", "auto"]
PERMISSION_MODES = set(PERMISSION_MODE_ORDER)


def listSlashCommands(args: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    args = {**(args or {}), **kwargs}
    commands = [
        ("help", "/help", "Show available slash commands"),
        ("exit", "/exit", "Exit the session"),
        ("quit", "/quit", "Exit the session"),
        ("clear", "/clear", "Clear the transcript view"),
        ("status", "/status", "Show session metadata"),
        ("providers", "/providers", "List configured provider profiles"),
        ("provider", "/provider [name]", "Show or switch the active provider profile"),
        ("model", "/model NAME", "Switch models for this session"),
        ("effort", "/effort LEVEL", "Set the reasoning effort for this session"),
        ("permissions", "/permissions [mode]", "View or update the permission mode"),
        ("agents", "/agents [running|all|inspect|attach|stop|retry <id>]", "Show and control child agents"),
        ("plans", "/plans [status]", "Show plan approvals linked to this session"),
        ("todos", "/todos", "Show the current todo list"),
        ("memory", "/memory", "Show project memory"),
        ("extensions", "/extensions", "Show MCP/plugin/runtime extension state"),
        ("daemon", "/daemon [restart]", "Show or control local task-supervisor daemon state"),
        ("remote", "/remote", "Show remote, bridge, and platform session state"),
        ("tasks", "/tasks [inspect|log|result|attach|stop|retry <id>]", "Show and control runtime tasks"),
        ("background", "/background [inspect|attach] [id]", "Show or attach background workers, monitors, and remote activity"),
        ("review", "/review [scope]", "Run a reviewer-style pass in the current session"),
        ("cost", "/cost", "Show tracked token/tool usage"),
        ("monitors", "/monitors", "List active and recent background monitors"),
        ("monitor", "/monitor <stop|resume> <id>", "Manage a specific background monitor"),
        ("resume", "/resume [query]", "List or switch recent sessions"),
        ("compact", "/compact [status|debug]", "Summarize older context and trim it"),
        ("transcript", "/transcript [limit]", "Render saved transcript entries"),
        ("debug", "/debug [output-dir]", "Create a debug bundle for this session"),
        ("diagnostics", "/diagnostics", "Show runtime diagnostic checks"),
        ("collaboration", "/collaboration", "Show workers, monitors, remote, and bridge status"),
        ("bug", "/bug", "Show the feedback URL"),
    ]
    if args.get("includeStatusline"):
        commands.append(("statusline", "/statusline [on|off]", "Toggle the footer line"))
    if args.get("includeTheme"):
        commands.append(("theme", "/theme [name|next]", "Switch the TUI color theme"))
    if args.get("includeMouse"):
        commands.append(("mouse", "/mouse [on|off|toggle]", "Toggle mouse capture vs native terminal selection"))
    if args.get("includeScreen"):
        commands.append(("screen", "/screen [auto|fullscreen|inline]", "Switch between inline scrollback and fullscreen transcript rendering"))
    for command in args.get("runtimeCommands") or []:
        name = command.get("name") if isinstance(command, dict) else getattr(command, "name", None)
        if not name:
            continue
        usage = command.get("usage") if isinstance(command, dict) else getattr(command, "usage", None)
        description = command.get("description") if isinstance(command, dict) else getattr(command, "description", "")
        commands.append((name, usage or f"/{name}", description))
    return [{"name": name, "usage": usage, "description": description} for name, usage, description in commands]


def getSlashCommandAvailability(args: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    args = {**(args or {}), **kwargs}
    hooks = args.get("hooks") or {}
    mode = str(args.get("mode") or ("bare" if args.get("bare") else "tui")).lower()
    commands = listSlashCommands(
        {
            "includeStatusline": True,
            "includeTheme": True,
            "includeMouse": True,
            "includeScreen": True,
            "runtimeCommands": args.get("runtimeCommands") or [],
        }
    )
    interactive_modes = {"tui", "interactive", "fullscreen", "inline"}
    availability = []
    for command in commands:
        name = command["name"]
        available = True
        reason = None
        if name in {"statusline", "theme", "mouse", "screen"} and mode not in interactive_modes:
            available = False
            reason = "full-screen TUI required"
        if name == "review" and not callable(hooks.get("runPrompt")):
            available = False
            reason = "interactive runtime required"
        if name == "extensions" and not callable(hooks.get("getExtensionsReport")):
            available = False
            reason = "extension report unavailable"
        availability.append({**command, "mode": mode, "available": available, "reason": reason})
    return availability


async def handleSlashCommand(args: dict[str, Any] | str | None = None, **kwargs: Any) -> dict[str, Any]:
    if isinstance(args, str):
        args = {"command": args, **kwargs}
    else:
        args = {**(args or {}), **kwargs}
    raw = (args.get("command") or args.get("input") or "").strip()
    if not raw.startswith("/"):
        return {"outcome": "pass"}
    command, arg_text = _parse_command(raw)
    session = args.get("session") or {}
    config = args.get("config") or {}
    hooks = args.get("hooks") or {}
    runtime_commands = args.get("runtimeCommands") or []
    if not command:
        return _handled("")

    runtime_command = _find_runtime_command(command, runtime_commands)
    _sync_effective_provider(session, config)
    if command == "help":
        return _info(hooks, "Commands", buildSlashHelp({"hooks": hooks, "runtimeCommands": runtime_commands}))
    if command in {"exit", "quit"}:
        return {"outcome": "exit"}
    if command == "clear":
        _call(hooks, "clear")
        return _handled("cleared")
    if command == "status":
        return _info(hooks, "Status", buildStatusReport(session, config))
    if command == "providers":
        return _info(hooks, "Providers", buildProviderProfilesReport(session, config))
    if command == "provider":
        return await _handle_provider(arg_text, session, config, hooks)
    if command == "model":
        return await _handle_model(arg_text, session, config, hooks)
    if command == "effort":
        return await _handle_effort(arg_text, session, config, hooks)
    if command == "permissions":
        return await _handle_permissions(arg_text, session, config, hooks)
    if command == "todos":
        return _info(hooks, "Todos", buildTodosReport(session))
    if command == "memory":
        return await _handle_memory(arg_text, session, hooks)
    if command == "extensions":
        return _handle_extensions(hooks)
    if command == "cost":
        return _info(hooks, "Usage", formatUsageBreakdown(session.get("metadata", {}).get("usage")))
    if command == "plans":
        return await _handle_plans(arg_text, session, hooks)
    if command == "tasks":
        return await _handle_tasks(arg_text, session, hooks, args)
    if command == "agents":
        return await _handle_agents(arg_text, session, config, hooks, args)
    if command == "background":
        return await _handle_background(arg_text, session, hooks)
    if command == "daemon":
        return await _handle_daemon(arg_text, session, config, hooks, args)
    if command == "remote":
        return await _handle_remote(arg_text, session, hooks)
    if command == "monitors":
        return _info(hooks, "Monitors", await buildMonitorsReport(session))
    if command == "monitor":
        return await _handle_monitor(arg_text, session, hooks)
    if command == "resume":
        return await _handle_resume(arg_text, session, hooks)
    if command == "compact":
        return await _handle_compact(arg_text, session, config, hooks, args)
    if command == "transcript":
        return await _handle_transcript(arg_text, session, hooks)
    if command == "debug":
        return await _handle_debug(arg_text, session, config, hooks)
    if command == "diagnostics":
        return await _handle_diagnostics(arg_text, session, config, hooks)
    if command == "collaboration":
        return await _handle_collaboration(arg_text, session, hooks)
    if command == "review":
        return await _handle_review(arg_text, hooks)
    if command == "statusline":
        return _handle_statusline(arg_text, hooks)
    if command == "theme":
        return _handle_theme(arg_text, hooks)
    if command == "mouse":
        return _handle_mouse(arg_text, hooks)
    if command == "screen":
        return _handle_screen(arg_text, hooks)
    if command == "bug":
        return _info(hooks, "Feedback", "Report issues at https://github.com/drost-ai/tess-core/issues")
    if runtime_command:
        result = runtime_command["handler"]({"argText": arg_text, "args": arg_text, "session": session, "config": config, "hooks": hooks})
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, dict):
            return result
        if result in {"handled", "exit", "pass"}:
            return {"outcome": result}
        return _handled(result or "")
    return _error(hooks, f"Unknown slash command: /{command}")


def buildSlashHelp(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    hooks = args.get("hooks") or {}
    lines = [
        f"{command['usage']:<22} {command['description']}"
        for command in listSlashCommands(
            {
                "includeStatusline": bool(hooks.get("getStatusLineVisible") and hooks.get("setStatusLineVisible")),
                "includeTheme": bool(hooks.get("getTheme") and hooks.get("setTheme")),
                "includeMouse": bool(hooks.get("getMouseMode") and hooks.get("setMouseMode")),
                "includeScreen": bool(hooks.get("getScreenMode") and hooks.get("setScreenMode")),
                "runtimeCommands": args.get("runtimeCommands") or [],
            }
        )
    ]
    for index, line in [
        (11, "/memory append TEXT    Append text to project memory"),
        (12, "/memory set TEXT       Replace project memory contents"),
        (13, "/memory clear          Delete project memory"),
        (8, "/agents running        Show active child agents for this session"),
        (9, "/agents all            Show all child agents for this session"),
        (10, "/agents inspect ID     Inspect a child agent"),
        (11, "/agents attach ID      Attach to a child agent session"),
        (12, "/agents stop ID        Stop a daemon-backed child agent"),
        (13, "/agents retry ID       Retry a daemon-backed child agent"),
        (22, "/tasks log ID          Show recent task output"),
        (23, "/tasks result ID       Show the latest task result surface"),
        (24, "/tasks attach ID       Reconnect to an attachable task session"),
        (25, "/tasks stop ID         Stop a daemon-backed task"),
        (26, "/tasks retry ID        Retry a retryable task"),
        (27, "/remote list           List known remote sessions"),
        (28, "/remote tail ID        Show recent bridge events"),
        (29, "/remote queue TEXT     Queue bridge input for the active remote"),
        (30, "/remote close ID       Close a remote bridge/session record"),
        (31, "/compact status        Show checkpoint and artifact state"),
        (32, "/compact debug         Create a debug bundle with compaction artifacts"),
    ]:
        lines.insert(index, line)
    return "\n".join(lines)


def buildStatusReport(session: dict[str, Any], config: dict[str, Any]) -> str:
    metadata = session.get("metadata", {})
    provider = session.get("provider") or config.get("provider")
    compaction_state = metadata.get("compactionState") or {}
    token_measurement = compaction_state.get("workingContextTokenMeasurement") or {}
    full_log = metadata.get("fullTranscriptLog") or {}
    return "\n".join(
        [
            f"session      {session.get('id', 'n/a')}",
            f"cwd          {session.get('cwd') or config.get('cwd') or 'n/a'}",
            f"profile      {session.get('providerProfileName') or config.get('providerProfileName') or 'n/a'}",
            f"provider     {provider or 'n/a'}",
            f"model        {session.get('model') or config.get('model') or 'n/a'}",
            f"mode         {config.get('openaiApiMode') if provider == 'openai' else 'messages'}",
            f"base url     {(config.get('openaiBaseUrl') or 'default') if provider == 'openai' else 'default'}",
            f"effort       {session.get('effort') or config.get('effort') or 'n/a'}",
            f"permissions  {session.get('permissionMode') or config.get('permissionMode') or 'n/a'}",
            f"messages     {len(session.get('messages') or [])}",
            f"todos        {len(session.get('todos') or [])}",
            f"monitors     {len(metadata.get('monitorIds') or [])}",
            f"usage        {formatUsageSummary(metadata.get('usage'))}",
            f"compactions  {metadata.get('compactions', 0)}",
            f"working ctx  {compaction_state.get('workingContextTokenEstimate', 'n/a')}",
            f"ctx source   {token_measurement.get('source', 'n/a')}",
            f"preflight    {formatPreflightTokenSummary(session)}",
            f"compacted to {compaction_state.get('compactedThroughMessageIndex', 'n/a')}",
            f"archived     {full_log.get('archivedMessageCount', 0)}",
            f"archive log  {full_log.get('path', 'n/a')}",
        ]
    )


def buildProviderProfilesReport(session: dict[str, Any], config: dict[str, Any]) -> str:
    profiles = config.get("providerProfiles") or {}
    active = session.get("providerProfileName") or config.get("providerProfileName")
    sections = []
    for name in listProviderProfiles(profiles):
        profile = profiles.get(name, {})
        if not profile:
            continue
        kind = profile.get("kind") or ""
        lines = [f"{name}{'  (active)' if name == active else ''}", f"  kind       {kind}"]
        if kind == "openai":
            lines.append(f"  mode       {profile.get('apiMode') or 'responses'}")
            lines.append(f"  base url   {profile.get('baseUrl') or 'default'}")
        lines.append(f"  model      {profile.get('defaultModel') or getDefaultModelForProvider(kind)}")
        auth = profile.get("auth") or {}
        auth_text = str(auth.get("kind") or "")
        if auth.get("envVar"):
            auth_text = f"{auth_text} via {auth.get('envVar')}"
        lines.append(f"  auth       {auth_text}")
        if profile.get("description"):
            lines.append(f"  about      {profile.get('description')}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else "No provider profiles configured."


async def buildPlansReport(session: dict[str, Any], status_filter: str = "") -> str:
    approvals = [plan for plan in await listPlanApprovals() if _is_related_plan_approval(session, plan)]
    normalized = status_filter.strip().lower()
    current_state = session.get("metadata", {}).get("planMode", {}).get("state") or "inactive"
    if not normalized or normalized == "pending":
        visible = [plan for plan in approvals if plan.get("status") == "pending"]
    elif normalized == "all":
        visible = approvals
    elif normalized in {"approved", "rejected"}:
        visible = [plan for plan in approvals if plan.get("status") == normalized]
    else:
        visible = [plan for plan in approvals if _matches_plan_query(plan, normalized)]
    if not visible:
        detail = f"No plan approvals matched: {status_filter}" if normalized and normalized != "pending" else "No pending plan approvals linked to this session."
        return "\n".join([f"current state  {current_state}", detail])
    return "\n".join(
        [
            f"current state  {current_state}",
            "",
            "\n\n".join(formatPlanApprovalRecord(plan) for plan in visible[:10]),
        ]
    )


async def _handle_plans(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    return _info(hooks, "Plans", await buildPlansReport(session, arg_text))


async def buildTasksReport(session: dict[str, Any], arg_text: str = "", args: dict[str, Any] | None = None) -> str:
    records = await listSessionTaskRecords(session.get("id"))
    parts = arg_text.split()
    if not parts:
        return formatTaskListReport(records)
    if parts[0] == "inspect" and len(parts) > 1:
        record = _resolve_task_record(records, parts[1]) or await loadTaskRecord(parts[1])
        return formatTaskInspectReport(record) if record else f"Task not found: {parts[1]}"
    if parts[0] == "result" and len(parts) > 1:
        record = _resolve_task_record(records, parts[1]) or await loadTaskRecord(parts[1])
        if not record:
            return f"Task not found: {parts[1]}"
        output, truncated = await _read_task_output(record, args)
        return formatTaskResultReport(record, output, truncated)
    if parts[0] == "log" and len(parts) > 1:
        record = _resolve_task_record(records, parts[1]) or {"id": parts[1], "title": parts[1]}
        output, truncated = await _read_task_output(record, args)
        return "\n".join(
            [
                f"{record.get('id')} - {record.get('title')}",
                "",
                *(["[truncated to recent output]"] if truncated else []),
                output or "(no output yet)",
            ]
        )
    return "Usage: /tasks [inspect|log|result|attach|stop|retry <id>]"


async def _handle_tasks(arg_text: str, session: dict[str, Any], hooks: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    records = await listSessionTaskRecords(session.get("id"))
    if not arg_text:
        return _info(hooks, "Tasks", formatTaskListReport(records))
    raw_subcommand, query = _split_subcommand(arg_text)
    subcommand = raw_subcommand.strip().lower()
    if subcommand not in {"inspect", "log", "result", "attach", "stop", "retry"} or not query:
        return _error(hooks, "Usage: /tasks [inspect|log|result|attach|stop|retry <id>]")
    record = _resolve_task_record(records, query)
    if not record:
        return _error(hooks, f"No task matched: {query}")
    if subcommand == "inspect":
        return _info(hooks, "Task", formatTaskInspectReport(record))
    if subcommand == "attach":
        if not record.get("attachableSessionId"):
            return _error(hooks, f"Task {record.get('id')} is retrieve-only and cannot be attached.")
        request_resume = hooks.get("requestResume") if isinstance(hooks, dict) else None
        if not callable(request_resume):
            return _error(hooks, "/tasks attach is only available in the full-screen TUI.")
        target_session = await loadSession(record["attachableSessionId"])
        if not target_session:
            return _error(hooks, f"Attach target not found: {record['attachableSessionId']}")
        if target_session.get("id") == session.get("id"):
            return _info(hooks, "Task", f"Task {record.get('id')} belongs to the current session. Use /tasks result {record.get('id')} or keep watching here.")
        await _persist(hooks)
        request_resume(target_session)
        return _handled(f"resuming {target_session.get('id')}")
    if subcommand in {"log", "result"}:
        output, truncated = await _read_task_output(record, args)
        if subcommand == "result":
            return _info(hooks, "Task Result", formatTaskResultReport(record, output, truncated))
        return _info(
            hooks,
            "Task Log",
            "\n".join(
                [
                    f"{record.get('id')} - {record.get('title')}",
                    "",
                    *(["[truncated to recent output]"] if truncated else []),
                    output or "(no output yet)",
                ]
            ),
        )
    supervisor, should_close = await _resolve_task_supervisor(args)
    if not supervisor:
        return _error(hooks, "This task action requires the local task supervisor daemon.")
    try:
        if subcommand == "stop":
            stop_fn = supervisor.get("stopTask") if isinstance(supervisor, dict) else None
            if not callable(stop_fn):
                return _error(hooks, "Task supervisor does not support stopTask.")
            stopped = await _maybe_await(stop_fn(record["id"]))
            if not stopped:
                return _error(hooks, f"Task could not be stopped: {record['id']}")
            return _info(hooks, "Task", f"Stopped {record['id']}")
        retry_fn = supervisor.get("retryTask") if isinstance(supervisor, dict) else None
        if not callable(retry_fn):
            return _error(hooks, "Task supervisor does not support retryTask.")
        try:
            retried = await _maybe_await(retry_fn(record["id"]))
        except Exception as exc:
            return _error(hooks, str(exc))
        return _info(
            hooks,
            "Task",
            "\n".join(
                [
                    f"Retried {record['id']}",
                    f"new task     {(retried or {}).get('taskId') or (retried or {}).get('id')}",
                    *([f"output       {retried.get('outputFile')}"] if isinstance(retried, dict) and retried.get("outputFile") else []),
                ]
            ),
        )
    finally:
        await _close_task_supervisor_if_needed(supervisor, should_close)


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


async def buildDaemonReport() -> str:
    daemon = await loadDaemonState()
    if not daemon:
        return "Daemon not running."
    return "\n".join(f"{key:<13} {daemon.get(key)}" for key in ("phase", "pid", "socketPath", "supervisorId", "epoch", "attachedClients", "watchedSessions", "activeTasks", "updatedAt"))


async def _handle_daemon(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    command = arg_text.strip().lower()
    controller = args.get("daemonController") or {}
    if command == "restart":
        restart_fn = controller.get("restart") if isinstance(controller, dict) else None
        if callable(restart_fn):
            await _maybe_await(restart_fn())
        else:
            await stopTaskSupervisorDaemon()
            await ensureTaskSupervisorDaemon({"cwd": session.get("cwd") or config.get("cwd") or os.getcwd()})
    return _info(hooks, "Daemon", await buildDaemonReport())


async def _handle_remote(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    metadata = session.get("metadata", {})
    remote = await loadRemoteSession(metadata.get("remoteSessionId")) if metadata.get("remoteSessionId") else None
    bridge = await loadBridge(metadata.get("bridgeId")) if metadata.get("bridgeId") else None
    if not arg_text:
        if remote or bridge:
            return _info(hooks, "Remote", buildRemoteReport(session, remote, bridge))
        fallback = _call_value(hooks, "getRemoteReport")
        return _info(hooks, "Remote", str(fallback) if fallback is not None else buildRemoteFallbackReport(session))

    subcommand, query = _split_subcommand(arg_text)
    if subcommand == "list":
        return _info(hooks, "Remote", await buildRemoteListReport())
    if subcommand in {"inspect", "show"}:
        selection = await _resolve_remote_record(query, session, remote)
        if not selection:
            return _error(hooks, f"No remote session matched: {query or 'current'}")
        linked_bridge = await loadBridge(selection.get("bridgeId")) if selection.get("bridgeId") else bridge
        return _info(hooks, "Remote", formatRemoteSessionRecord(selection, linked_bridge))
    if subcommand in {"tail", "events"}:
        selected_bridge = await _resolve_bridge_record(query, session, bridge)
        if not selected_bridge:
            return _error(hooks, f"No bridge matched: {query or 'current'}")
        return _info(hooks, "Bridge", await formatBridgeTail(selected_bridge["id"]))
    if subcommand in {"queue", "send", "message"}:
        if not query:
            return _error(hooks, "Usage: /remote queue TEXT")
        selected_bridge = await _resolve_bridge_record("", session, bridge)
        if not selected_bridge and remote and remote.get("bridgeId"):
            selected_bridge = await loadBridge(remote["bridgeId"])
        if not selected_bridge:
            return _error(hooks, "No active bridge is available for this session.")
        try:
            message = await sendBridgeMessage({"bridgeId": selected_bridge["id"], "text": query, "source": "local"})
        except Exception as exc:
            return _error(hooks, str(exc))
        return _info(hooks, "Bridge", f"Queued {message.get('id')} for bridge {_short_id(selected_bridge['id'])}")
    if subcommand in {"close", "disconnect"}:
        selection = await _resolve_remote_record(query, session, remote)
        selected_bridge = await _resolve_bridge_record(query, session, bridge)
        if not selection and not selected_bridge:
            return _error(hooks, f"No remote or bridge matched: {query or 'current'}")
        await _close_remote_records(selection, selected_bridge)
        _call(hooks, "refresh")
        await _persist(hooks)
        return _info(hooks, "Remote", f"Closed {selection.get('id') if selection else selected_bridge.get('id')}")
    if subcommand not in {"attach", "resume"}:
        return _error(hooks, "Usage: /remote [list|inspect|tail|attach|resume|queue|close]")
    request_resume = hooks.get("requestResume") if isinstance(hooks, dict) else None
    if not callable(request_resume):
        return _error(hooks, "/remote attach is only available in the full-screen TUI.")
    target_session_id = await _resolve_remote_attach_target(query, session, remote)
    if not target_session_id:
        return _error(hooks, "No attachable remote session is available.")
    target_session = await loadSession(target_session_id)
    if not target_session:
        return _error(hooks, f"Remote session target not found: {target_session_id}")
    await _persist(hooks)
    request_resume(target_session)
    return _handled(f"resuming {target_session_id}")


async def _handle_background(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    agents = [record for record in await listChildAgents() if _is_related_child_agent(session, record)]
    monitors = await listSessionMonitors(session.get("id"))
    tasks = [
        record
        for record in await listSessionTaskRecords(session.get("id"))
        if record.get("kind") in {"background_command", "main_session"}
    ]
    metadata = session.get("metadata", {})
    remote = await loadRemoteSession(metadata.get("remoteSessionId")) if metadata.get("remoteSessionId") else None
    bridge = await loadBridge(metadata.get("bridgeId")) if metadata.get("bridgeId") else None

    if not arg_text:
        return _info(hooks, "Background", buildBackgroundReport(session, agents, monitors, tasks, remote, bridge))

    subcommand, query = _split_subcommand(arg_text)
    if not subcommand or not query:
        return _error(hooks, "Usage: /background [inspect|attach|resume] <id>")
    selection = _resolve_background_selection(agents, monitors, tasks, remote, bridge, query)
    if not selection:
        return _error(hooks, f"No background item matched: {query}")

    if subcommand == "inspect":
        return _info(hooks, "Background", _format_background_selection(selection))
    if subcommand not in {"attach", "resume"}:
        return _error(hooks, "Usage: /background [inspect|attach|resume] <id>")

    request_resume = hooks.get("requestResume") if isinstance(hooks, dict) else None
    if not callable(request_resume):
        return _error(hooks, "/background attach is only available in the full-screen TUI.")
    target_session_id = _background_attach_target(selection)
    if not target_session_id:
        return _error(hooks, f"No attachable session is available for {selection['label']}.")
    target_session = await loadSession(target_session_id)
    if not target_session:
        return _error(hooks, f"Attach target not found: {target_session_id}")
    await _persist(hooks)
    request_resume(target_session)
    return _handled(f"resuming {target_session_id}")


def buildRemoteReport(
    session: dict[str, Any],
    remote: dict[str, Any] | None = None,
    bridge: dict[str, Any] | None = None,
) -> str:
    sections = [buildRemoteFallbackReport(session)]
    if remote:
        sections.append(formatRemoteSessionRecord(remote, bridge))
    if bridge and not remote:
        sections.append(
            "\n".join(
                [
                    f"{_short_id(bridge.get('id', ''))}  {bridge.get('status')}  {bridge.get('name') or 'bridge'}",
                    f"updated      {str(bridge.get('updatedAt', ''))[:19]}",
                    f"last event   {bridge.get('lastEventName') or 'n/a'}",
                    f"pending      {bridge.get('pendingMessages') or 0}",
                ]
            )
        )
    return "\n\n".join(sections)


def buildRemoteFallbackReport(session: dict[str, Any]) -> str:
    metadata = session.get("metadata", {})
    lines = [
        f"session        {session.get('id')}",
        f"cwd            {session.get('cwd')}",
        f"remote         {metadata.get('remoteSessionId') or 'none'}",
        f"bridge         {metadata.get('bridgeId') or 'none'}",
        f"channels       {', '.join(metadata.get('channelServers') or []) or 'none'}",
        f"tmux           {metadata.get('tmuxSessionName') or 'none'}",
        f"ide            {metadata.get('ideName') or 'none'}",
        f"chrome         {metadata.get('chromeIntegrationMode') or 'off'}",
    ]
    if metadata.get("remoteSessionTitle"):
        lines.insert(3, f"title          {metadata.get('remoteSessionTitle')}")
    return "\n".join(lines)


async def buildRemoteListReport() -> str:
    remotes = await listRemoteSessions()
    if not remotes:
        return "No remote sessions recorded."
    return "\n".join(formatRemoteSessionCandidate(record) for record in remotes[:12])


async def _resolve_remote_record(
    query: str,
    session: dict[str, Any],
    current_remote: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    if not normalized:
        return current_remote
    if current_remote and any(normalized in token for token in _background_remote_tokens(current_remote)):
        return current_remote
    for record in await listRemoteSessions():
        if any(normalized in token for token in _background_remote_tokens(record)):
            return record
    session_match = await loadSession(query)
    if session_match:
        for record in await listRemoteSessions():
            if record.get("sessionId") == session_match.get("id") or record.get("foregroundSessionId") == session_match.get("id"):
                return record
    metadata = session.get("metadata", {})
    if metadata.get("remoteSessionId") and normalized in str(metadata.get("remoteSessionId")).lower():
        return await loadRemoteSession(metadata["remoteSessionId"])
    return None


async def _resolve_bridge_record(
    query: str,
    session: dict[str, Any],
    current_bridge: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    if not normalized:
        return current_bridge
    if current_bridge and any(normalized in token for token in _compact_tokens(current_bridge.get("id"), current_bridge.get("name"), current_bridge.get("remoteSessionId"), current_bridge.get("sessionId"), current_bridge.get("status"))):
        return current_bridge
    remote = await _resolve_remote_record(query, session, None)
    if remote and remote.get("bridgeId"):
        bridge = await loadBridge(remote["bridgeId"])
        if bridge:
            return bridge
    for bridge in await listBridges():
        tokens = _compact_tokens(bridge.get("id"), bridge.get("name"), bridge.get("remoteSessionId"), bridge.get("sessionId"), bridge.get("status"))
        if any(normalized in token for token in tokens):
            return bridge
    metadata = session.get("metadata", {})
    if metadata.get("bridgeId") and normalized in str(metadata.get("bridgeId")).lower():
        return await loadBridge(metadata["bridgeId"])
    return None


async def _close_remote_records(remote: dict[str, Any] | None, bridge: dict[str, Any] | None) -> None:
    timestamp = now_iso()
    if bridge:
        bridge = {
            **bridge,
            "status": "closed",
            "updatedAt": timestamp,
            "lastEventName": "bridge.closed",
        }
        await saveBridge(bridge)
    if remote:
        await updateRemoteSession(
            remote["id"],
            {
                "status": "idle",
                "bridgeStatus": "closed" if bridge else remote.get("bridgeStatus"),
                "lastEventName": "remote.closed" if not bridge else "bridge.closed",
                "lastEventAt": timestamp,
                "lastEventSummary": "Remote shell record closed.",
                "unreadBridgeMessages": 0,
            },
        )
    elif bridge and bridge.get("remoteSessionId"):
        await updateRemoteSession(
            bridge["remoteSessionId"],
            {
                "status": "idle",
                "bridgeStatus": "closed",
                "lastEventName": "bridge.closed",
                "lastEventAt": timestamp,
                "lastEventSummary": "Remote shell bridge closed.",
                "unreadBridgeMessages": 0,
            },
        )


def buildBackgroundReport(
    session: dict[str, Any],
    agents: list[dict[str, Any]],
    monitors: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    remote: dict[str, Any] | None = None,
    bridge: dict[str, Any] | None = None,
) -> str:
    active_agents = [record for record in agents if _is_active_child_agent(record)]
    active_tasks = [record for record in tasks if _is_active_task_record(record)]
    lines = [
        f"session        {session.get('id')}",
        f"background     {len(session.get('metadata', {}).get('backgroundSessionIds') or [])}",
        f"tasks          {len(active_tasks)}/{len(tasks)}",
        f"workers        {len(active_agents)}/{len(agents)}",
        f"monitors       {sum(1 for record in monitors if record.get('status') == 'active')}/{len(monitors)}",
        f"remote         {_short_id(remote.get('id', '')) + ' ' + str(remote.get('status')) if remote else 'none'}",
    ]
    sections = ["\n".join(lines)]
    if active_tasks:
        sections.append("\n".join(["Tasks:", *[f"- {_format_background_line(formatTaskInspectReport(record))}" for record in active_tasks[:8]]]))
    if active_agents:
        sections.append("\n".join(["Workers:", *[f"- {_format_background_line(formatChildAgentRecord(record))}" for record in active_agents[:8]]]))
    if monitors:
        sections.append("\n".join(["Monitors:", *[f"- {_format_background_line(formatMonitorRecord(record))}" for record in monitors[:6]]]))
    if remote:
        sections.append("\n".join(["Remote:", *[f"- {line}" for line in formatRemoteSessionRecord(remote, bridge).splitlines()]]))
    sections.append("tip            /background inspect <id> or /background attach <id>")
    return "\n\n".join(sections)


async def _resolve_remote_attach_target(query: str, session: dict[str, Any], remote: dict[str, Any] | None) -> str | None:
    metadata = session.get("metadata", {})
    if not query:
        return (remote or {}).get("foregroundSessionId") or (remote or {}).get("sessionId")
    remote_id = str(metadata.get("remoteSessionId") or "")
    if remote_id and query.lower() in remote_id.lower():
        return (remote or {}).get("foregroundSessionId") or (remote or {}).get("sessionId")
    saved = await loadSession(query)
    return (saved or {}).get("id")


def _resolve_background_selection(
    agents: list[dict[str, Any]],
    monitors: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    remote: dict[str, Any] | None,
    bridge: dict[str, Any] | None,
    query: str,
) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    ordered = [
        ("task", tasks, _background_task_tokens, lambda record: record.get("title")),
        ("agent", agents, _background_agent_tokens, lambda record: record.get("name") or record.get("agentType") or record.get("agentId")),
        ("monitor", monitors, _background_monitor_tokens, lambda record: record.get("goal")),
    ]
    for kind, records, tokenizer, labeler in ordered:
        for record in records:
            if normalized in tokenizer(record):
                return {"kind": kind, "label": labeler(record), "record": record}
    if remote and normalized in _background_remote_tokens(remote, bridge):
        return {"kind": "remote", "label": remote.get("title"), "record": remote, "bridge": bridge}
    for kind, records, tokenizer, labeler in ordered:
        for record in records:
            if any(normalized in token for token in tokenizer(record)):
                return {"kind": kind, "label": labeler(record), "record": record}
    if remote and any(normalized in token for token in _background_remote_tokens(remote, bridge)):
        return {"kind": "remote", "label": remote.get("title"), "record": remote, "bridge": bridge}
    return None


def _format_background_selection(selection: dict[str, Any]) -> str:
    if selection.get("kind") == "task":
        return formatTaskInspectReport(selection["record"])
    if selection.get("kind") == "agent":
        return formatChildAgentRecord(selection["record"])
    if selection.get("kind") == "monitor":
        return formatMonitorRecord(selection["record"])
    return formatRemoteSessionRecord(selection["record"], selection.get("bridge"))


def _background_attach_target(selection: dict[str, Any]) -> str | None:
    record = selection.get("record") or {}
    if selection.get("kind") == "agent":
        return record.get("sessionId")
    if selection.get("kind") == "task":
        return record.get("attachableSessionId")
    if selection.get("kind") == "remote":
        return record.get("foregroundSessionId") or record.get("sessionId")
    return None


def _background_task_tokens(record: dict[str, Any]) -> list[str]:
    return _compact_tokens(
        record.get("id"),
        record.get("sourceId"),
        record.get("kind"),
        record.get("status"),
        record.get("title"),
        record.get("summary"),
        record.get("detailStatus"),
        record.get("attachableSessionId"),
        record.get("command"),
    )


def _background_agent_tokens(record: dict[str, Any]) -> list[str]:
    return _compact_tokens(
        record.get("agentId"),
        record.get("sessionId"),
        record.get("taskId"),
        record.get("name"),
        record.get("agentType"),
        record.get("teamName"),
        record.get("status"),
    )


def _background_monitor_tokens(record: dict[str, Any]) -> list[str]:
    return _compact_tokens(record.get("id"), record.get("taskId"), record.get("goal"), record.get("status"))


def _background_remote_tokens(record: dict[str, Any], bridge: dict[str, Any] | None = None) -> list[str]:
    return _compact_tokens(
        record.get("id"),
        record.get("sessionId"),
        record.get("foregroundSessionId"),
        record.get("title"),
        record.get("description"),
        record.get("status"),
        record.get("bridgeId"),
        (bridge or {}).get("id"),
        (bridge or {}).get("name"),
    )


def _compact_tokens(*values: Any) -> list[str]:
    return [str(value).lower() for value in values if value]


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


def _is_related_plan_approval(session: dict[str, Any], record: dict[str, Any]) -> bool:
    if record.get("sessionId") == session.get("id") or record.get("parentSessionId") == session.get("id"):
        return True
    agent_id = record.get("agentId")
    if agent_id and agent_id == session.get("metadata", {}).get("agentId"):
        return True
    return bool(agent_id and agent_id in (session.get("metadata", {}).get("childAgentIds") or []))


def _matches_plan_query(record: dict[str, Any], query: str) -> bool:
    return any(
        query in value
        for value in _compact_tokens(
            record.get("requestId"),
            record.get("sessionId"),
            record.get("parentSessionId"),
            record.get("agentId"),
            record.get("status"),
            record.get("filePath"),
            record.get("reason"),
            record.get("plan"),
        )
    )


def formatPlanApprovalRecord(record: dict[str, Any]) -> str:
    request_id = str(record.get("requestId") or "")
    origin = f"agent {_short_id(str(record.get('agentId')))}" if record.get("agentId") else "session"
    lines = [
        f"{_short_id(request_id)}  {record.get('status')}  {origin}",
        f"created      {str(record.get('createdAt') or '')[:19].replace('T', ' ')}",
    ]
    if record.get("resolvedAt"):
        lines.append(f"resolved     {str(record.get('resolvedAt'))[:19].replace('T', ' ')}")
    prompts = record.get("allowedPrompts") or []
    if prompts:
        formatted_prompts = []
        for prompt in prompts:
            if isinstance(prompt, dict):
                formatted_prompts.append(f"{prompt.get('tool')}({truncate(collapseWhitespace(prompt.get('prompt')), 48)})")
        if formatted_prompts:
            lines.append(f"prompts      {', '.join(formatted_prompts)}")
    if record.get("reason"):
        lines.append(f"reason       {record.get('reason')}")
    if record.get("filePath"):
        lines.append(f"file         {record.get('filePath')}")
    if record.get("plan"):
        lines.append(f"plan         {truncate(collapseWhitespace(record.get('plan')), 180)}")
    return "\n".join(lines)


async def _read_task_output(record: dict[str, Any], args: dict[str, Any] | None = None) -> tuple[str, bool]:
    supervisor, should_close = await _resolve_task_supervisor(args or {})
    try:
        read_output = supervisor.get("readTaskOutput") if isinstance(supervisor, dict) else None
        if callable(read_output):
            value = await _maybe_await(read_output(record.get("id"), 12000))
            if isinstance(value, dict):
                return str(value.get("output") or ""), bool(value.get("truncated"))
            return str(value or ""), False
    finally:
        await _close_task_supervisor_if_needed(supervisor, should_close)
    output_file = record.get("outputFile")
    if output_file:
        try:
            with open(output_file, "r", encoding="utf-8") as handle:
                return handle.read(), False
        except OSError:
            return "", False
    return "", False


def formatTaskListReport(records: list[dict[str, Any]]) -> str:
    if not records:
        return "No runtime tasks for this session."
    counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    summary = " - ".join(f"{status} {counts[status]}" for status in sorted(counts))
    lines = [f"summary      {summary}", ""]
    for record in records[:12]:
        lines.append(
            "\n".join(
                [
                    f"{str(record.get('id') or '').ljust(20)} {str(record.get('status') or '').ljust(12)} {record.get('kind')}",
                    f"title        {record.get('title')}",
                    *([f"family       {record.get('taskFamily')}"] if record.get("taskFamily") else []),
                    *([f"detail       {record.get('detailStatus')}"] if record.get("detailStatus") else []),
                    *([f"summary      {record.get('summary')}"] if record.get("summary") else []),
                    *([f"result       {record.get('resultSummary')}"] if record.get("resultSummary") else []),
                    f"surface      {formatTaskSurface(record)}",
                    *([f"owner        {record.get('owner')}"] if record.get("owner") else []),
                    f"updated      {record.get('updatedAt')}",
                ]
            )
        )
    lines.extend(["", "tip          /tasks inspect <id> - /tasks result <id> - /tasks attach <id> - /tasks log <id>"])
    return "\n".join(lines)


def formatTaskResultReport(record: dict[str, Any], output: str = "", truncated: bool = False) -> str:
    lines = [
        f"{record.get('id')} - {record.get('title')}",
        f"status       {record.get('status')}",
    ]
    if record.get("detailStatus"):
        lines.append(f"detail       {record.get('detailStatus')}")
    if record.get("resultSummary"):
        lines.append(f"result       {record.get('resultSummary')}")
    if record.get("summary") and record.get("summary") != record.get("resultSummary"):
        lines.append(f"summary      {record.get('summary')}")
    lines.append(f"surface      {formatTaskSurface(record)}")
    if record.get("attachableSessionId"):
        lines.append(f"attach       /tasks attach {record.get('id')}")
    if record.get("outputFile"):
        lines.append(f"output       {record.get('outputFile')}")
    if output.strip():
        lines.append("")
        if truncated:
            lines.append("[truncated to recent output]")
        lines.append(output.strip())
    return "\n".join(lines)


def formatTaskInspectReport(record: dict[str, Any]) -> str:
    lines = [
        f"id           {record.get('id')}",
        f"kind         {record.get('kind')}",
        f"status       {record.get('status')}",
        f"title        {record.get('title')}",
    ]
    for label, key in [
        ("family", "taskFamily"),
        ("detail", "detailStatus"),
        ("summary", "summary"),
        ("result", "resultSummary"),
    ]:
        if record.get(key):
            lines.append(f"{label:<12} {record.get(key)}")
    lines.append(f"surface      {formatTaskSurface(record)}")
    if record.get("attachableSessionId"):
        lines.append(f"attach       /tasks attach {record.get('id')}")
    for label, key in [
        ("owner", "owner"),
        ("supervisor", "supervisorId"),
        ("cwd", "cwd"),
        ("command", "command"),
        ("output", "outputFile"),
    ]:
        if record.get(key):
            lines.append(f"{label:<12} {record.get(key)}")
    if isinstance(record.get("attachmentCount"), int):
        lines.append(f"attachments  {record.get('attachmentCount')}")
    if isinstance(record.get("outputCursor"), int):
        lines.append(f"output bytes {record.get('outputCursor')}")
    if record.get("relatedMonitorIds"):
        lines.append(f"monitors     {', '.join(record.get('relatedMonitorIds') or [])}")
    if record.get("relatedChildSessionIds"):
        lines.append(f"children     {', '.join(record.get('relatedChildSessionIds') or [])}")
    lines.append(f"updated      {record.get('updatedAt')}")
    return "\n".join(lines)


def formatTaskSurface(record: dict[str, Any]) -> str:
    if record.get("attachableSessionId"):
        return f"attachable via session {_short_id(record.get('attachableSessionId', ''))}"
    if record.get("retrieveOnly"):
        return "retrieve-only"
    return "inspectable"


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


def formatRemoteSessionRecord(record: dict[str, Any], bridge: dict[str, Any] | None = None) -> str:
    lines = [
        f"{_short_id(record.get('id', ''))}  {record.get('status')}  {record.get('title')}",
        f"session      {_short_id(record.get('sessionId', ''))}",
        f"updated      {str(record.get('updatedAt') or '')[:19].replace('T', ' ')}",
        f"channels     {', '.join(record.get('channelServers') or []) or 'none'}",
    ]
    if record.get("foregroundSessionId"):
        lines.append(f"foreground   {_short_id(record.get('foregroundSessionId', ''))}")
    if record.get("lastEventName"):
        lines.append(f"last event   {record.get('lastEventName')}")
    if record.get("lastEventSummary"):
        lines.append(f"summary      {truncate(collapseWhitespace(record.get('lastEventSummary')), 180)}")
    if record.get("unreadBridgeMessages"):
        lines.append(f"queued       {record.get('unreadBridgeMessages')}")
    if bridge:
        lines.append(f"bridge       {_short_id(bridge.get('id', ''))}  {bridge.get('status')}")
        if bridge.get("lastEventName"):
            lines.append(f"bridge evt   {bridge.get('lastEventName')}")
        if bridge.get("pendingMessages"):
            lines.append(f"bridge q     {bridge.get('pendingMessages')}")
    return "\n".join(lines)


def _is_related_child_agent(session: dict[str, Any], record: dict[str, Any]) -> bool:
    if record.get("parentSessionId") == session.get("id") or record.get("sessionId") == session.get("id"):
        return True
    return record.get("agentId") in (session.get("metadata", {}).get("childAgentIds") or [])


def _is_active_child_agent(record: dict[str, Any]) -> bool:
    return record.get("status") in {"queued", "running", "backgrounded", "awaiting_plan_approval", "awaiting_input"}


def _is_active_task_record(record: dict[str, Any]) -> bool:
    return record.get("status") in {"queued", "running", "backgrounded", "awaiting_input", "reconciling", "degraded"}


def _format_background_line(text: str) -> str:
    return " | ".join(text.splitlines())


async def buildMonitorsReport(session: dict[str, Any]) -> str:
    monitors = await listSessionMonitors(session.get("id"))
    if not monitors:
        return "No background monitors are active for this session."
    return "\n\n".join(formatMonitorRecord(monitor) for monitor in monitors[:12])


async def _handle_memory(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    cwd = session.get("cwd") or os.getcwd()
    memory_path = getProjectMemoryPath(cwd)
    command, payload = _split_subcommand(arg_text)
    if not command or command == "show":
        records = await synchronizeManualProjectMemory(cwd)
        try:
            with open(memory_path, "r", encoding="utf-8") as handle:
                content = handle.read().strip()
        except FileNotFoundError:
            content = ""
        return _info(hooks, "Memory", "\n".join([f"path     {memory_path}", f"records  {len(records)}", "", content or "No memory stored for this project yet."]))
    if command == "clear":
        try:
            os.remove(memory_path)
        except FileNotFoundError:
            pass
        records = await synchronizeManualProjectMemory(cwd)
        return _info(hooks, "Memory", f"Cleared project memory at {memory_path}\nrecords  {len(records)}")
    if command not in {"set", "append"} or not payload:
        return _error(hooks, "Usage: /memory [show|append TEXT|set TEXT|clear]")
    os.makedirs(os.path.dirname(memory_path), exist_ok=True)
    if command == "set":
        next_content = f"{payload}\n"
    else:
        try:
            with open(memory_path, "r", encoding="utf-8") as handle:
                previous = handle.read().strip()
        except FileNotFoundError:
            previous = ""
        next_content = f"{previous}\n{payload}\n" if previous else f"{payload}\n"
    with open(memory_path, "w", encoding="utf-8") as handle:
        handle.write(next_content)
    records = await synchronizeManualProjectMemory(cwd)
    verb = "Replaced" if command == "set" else "Appended to"
    return _info(hooks, "Memory", f"{verb} project memory at {memory_path}\nrecords  {len(records)}")


def _handle_extensions(hooks: dict[str, Any]) -> dict[str, Any]:
    report = _call_value(hooks, "getExtensionsReport")
    if report is None:
        return _error(hooks, "/extensions is not available in this runtime.")
    return _info(hooks, "Extensions", str(report))


async def _handle_monitor(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    parts = arg_text.split(None, 1)
    if len(parts) != 2 or parts[0] not in {"stop", "resume"}:
        return _error(hooks, "Usage: /monitor <stop|resume> <monitor-id>")
    monitors = await listSessionMonitors(session.get("id"))
    monitor = _resolve_monitor(monitors, parts[1])
    if not monitor:
        return _error(hooks, f"No monitor matched: {parts[1]}")
    now = now_iso()
    monitor = {**monitor, "status": "stopped" if parts[0] == "stop" else "active", "updatedAt": now}
    if parts[0] == "stop":
        monitor["stoppedAt"] = now
    else:
        monitor.pop("stoppedAt", None)
    await saveMonitor(monitor)
    _call(hooks, "refresh")
    await _persist(hooks)
    return _info(hooks, "Monitor", f"{'Stopped' if parts[0] == 'stop' else 'Resumed'} monitor {monitor['id']}")


async def _handle_resume(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    candidates = [candidate for candidate in await listSessions() if candidate.get("cwd") == session.get("cwd") and candidate.get("id") != session.get("id")]
    if not arg_text:
        if not candidates:
            return _info(hooks, "Resume", "No other saved sessions found for this workspace.")
        return _info(hooks, "Resume", "\n".join(formatSessionCandidate(candidate) for candidate in candidates[:10]))
    matches = [candidate for candidate in candidates if _session_matches(candidate, arg_text)]
    if not matches:
        return _error(hooks, f"No saved session in this workspace matched: {arg_text}")
    if len(matches) > 1:
        return _info(hooks, "Resume", "\n".join(formatSessionCandidate(candidate) for candidate in matches[:10]))
    request_resume = hooks.get("requestResume") if isinstance(hooks, dict) else None
    if not callable(request_resume):
        return _error(hooks, "/resume switching is only available in the full-screen TUI.")
    await _persist(hooks)
    request_resume(matches[0])
    return _handled(f"resuming {matches[0].get('id')}")


async def _handle_compact(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    subcommand, payload = _split_subcommand(arg_text)
    if subcommand in {"status", "artifacts", "state"}:
        return _info(hooks, "Compact", await buildCompactionStatusReport(session))
    if subcommand == "debug":
        return await _handle_debug(payload, session, config, hooks, title="Compact Debug")
    if subcommand and subcommand not in {"run", "force"}:
        return _error(hooks, "Usage: /compact [status|debug]")
    if _call_value(hooks, "isCompacting"):
        return _info(hooks, "Compact", "Compaction is already running.")
    compaction_state = (session.get("metadata") or {}).get("compactionState") or {}
    token_measurement = compaction_state.get("workingContextTokenMeasurement") or {}
    _emit_runtime(
        hooks,
        "compaction.started",
        {
            "session_id": session.get("id"),
            "trigger": "manual",
            "current_token_estimate": compaction_state.get("workingContextTokenEstimate"),
            "current_token_source": token_measurement.get("source"),
        },
    )
    runner = hooks.get("runCompaction") if isinstance(hooks, dict) else None
    runner = runner if callable(runner) else runCompaction
    payload = {"session": session, "config": config, "trigger": "manual", "force": True}
    if args.get("modelClient") is not None:
        payload["modelClient"] = args["modelClient"]
    try:
        outcome = runner(payload)
        if hasattr(outcome, "__await__"):
            outcome = await outcome
    except Exception as exc:
        provider_failure = getattr(exc, "record", None)
        provider_failure = provider_failure if isinstance(provider_failure, dict) else {}
        _emit_runtime(
            hooks,
            "compaction.failed",
            {
                "session_id": session.get("id"),
                "trigger": "manual",
                "current_token_estimate": compaction_state.get("workingContextTokenEstimate"),
                "current_token_source": token_measurement.get("source"),
                "error": provider_failure.get("message") or str(exc),
                "provider": provider_failure.get("provider"),
                "provider_label": provider_failure.get("providerLabel"),
                "category": provider_failure.get("category"),
                "phase": provider_failure.get("phase"),
                "retryable": provider_failure.get("retryable"),
                "attempt": provider_failure.get("attempt"),
                "status": provider_failure.get("status"),
                "code": provider_failure.get("code"),
                "error_type": provider_failure.get("errorType"),
                "raw_error": provider_failure.get("rawMessage"),
            },
        )
        return _error(hooks, str(exc))
    if outcome.get("status") == "skipped":
        _emit_runtime(
            hooks,
            "compaction.skipped",
            {
                "session_id": session.get("id"),
                "trigger": "manual",
                "current_token_estimate": compaction_state.get("workingContextTokenEstimate"),
                "current_token_source": token_measurement.get("source"),
                "reason": outcome.get("reason"),
                "detail": outcome.get("detail"),
                "previous_cutoff": outcome.get("previousCutoff"),
                "candidate_cutoff": outcome.get("candidateCutoff"),
                "quality_score": outcome.get("qualityScore"),
                "quality_classification": outcome.get("qualityClassification"),
            },
        )
        lines = ["Checkpoint not created.", f"reason          {outcome.get('reason')}", f"detail          {outcome.get('detail')}"]
        if compaction_state:
            artifacts = compaction_state.get("artifacts") or {}
            lines.extend(
                [
                    f"latest gen      {compaction_state.get('latestCompactionGeneration')}",
                    f"summary         {artifacts.get('summary')}",
                    f"working ctx     {artifacts.get('workingContext')}",
                ]
            )
        return _info(hooks, "Compact", "\n".join(lines))
    _call(hooks, "refresh")
    await _persist(hooks)
    artifacts = outcome.get("artifacts") or {}
    compaction_model = outcome.get("compactionModel") or {}
    verification_model = outcome.get("verificationModel") or {}
    verification = outcome.get("verification") or {}
    working_measurement = outcome.get("workingContextTokenMeasurement") or {}
    _emit_runtime(
        hooks,
        "compaction.completed",
        {
            "session_id": session.get("id"),
            "generation": outcome.get("generation"),
            "trigger": outcome.get("trigger"),
            "compacted_through_message_index": outcome.get("compactedThroughMessageIndex"),
            "compacted_message_count": outcome.get("compactedMessageCount"),
            "recent_message_count": outcome.get("recentMessageCount"),
            "raw_token_estimate": outcome.get("rawTokenEstimate"),
            "working_context_token_estimate": outcome.get("workingContextTokenEstimate"),
            "working_context_token_source": working_measurement.get("source"),
            "quality_classification": outcome.get("qualityClassification"),
            "compaction_provider": compaction_model.get("provider"),
            "compaction_model": compaction_model.get("model"),
            "verification_provider": verification_model.get("provider"),
            "verification_model": verification_model.get("model"),
            "verification_verdict": verification.get("verdict"),
            "verification_confidence": verification.get("confidence"),
            "summary_path": artifacts.get("summary"),
            "state_path": artifacts.get("state"),
            "working_context_path": artifacts.get("workingContext"),
            "checkpoint_path": artifacts.get("checkpoint"),
        },
    )
    return _info(
        hooks,
        "Compact",
        "\n".join(
            [
                "Compacted working context.",
                "Archive preserved.",
                f"generation      {outcome.get('generation')}",
                f"compacted msgs  {outcome.get('compactedMessageCount')}",
                f"recent msgs     {outcome.get('recentMessageCount')}",
                f"working ctx     {outcome.get('workingContextTokenEstimate')}",
                f"quality         {outcome.get('qualityClassification')}",
                f"summary         {artifacts.get('summary', 'n/a')}",
                f"state           {artifacts.get('state', 'n/a')}",
                f"working ctx     {artifacts.get('workingContext', 'n/a')}",
                f"checkpoint      {artifacts.get('checkpoint', 'n/a')}",
                "Future prompts will use the compacted artifacts plus recent raw turns.",
            ]
        ),
    )


async def buildCompactionStatusReport(session: dict[str, Any]) -> str:
    metadata = session.get("metadata") or {}
    state = metadata.get("compactionState") or {}
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    measurement = state.get("workingContextTokenMeasurement") or {}
    manifest = await loadCompactionManifest(session.get("id")) if session.get("id") else None
    generation = state.get("latestCompactionGeneration")
    lines = [
        f"session        {session.get('id') or 'n/a'}",
        f"compactions    {metadata.get('compactions', 0)}",
        f"latest gen     {generation if generation is not None else 'n/a'}",
        f"working ctx    {state.get('workingContextTokenEstimate', 'n/a')}",
        f"ctx source     {measurement.get('source', 'n/a')}",
        f"compacted to   {state.get('compactedThroughMessageIndex', 'n/a')}",
        f"manifest       {compactionManifestPath(session.get('id')) if session.get('id') and manifest else 'none'}",
    ]
    if manifest:
        lines.extend(
            [
                f"manifest gen   {manifest.get('latestGeneration') or manifest.get('generation') or 'n/a'}",
                f"manifest ok    {manifest.get('status') or manifest.get('qualityClassification') or 'n/a'}",
            ]
        )
    for label, key in [
        ("summary", "summary"),
        ("state", "state"),
        ("working ctx", "workingContext"),
        ("activity", "activity"),
        ("checkpoint", "checkpoint"),
    ]:
        if artifacts.get(key):
            lines.append(f"{label:<14} {artifacts.get(key)}")
    if session.get("id") and isinstance(generation, int):
        lines.append(f"checkpoint src {compactionCheckpointPath(session['id'], generation)}")
    if not state and not manifest:
        lines.append("No compaction checkpoint recorded for this session.")
    return "\n".join(lines)


async def _handle_transcript(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    arg_text = arg_text.strip()
    limit = None
    if arg_text:
        try:
            limit = max(1, int(arg_text))
        except ValueError:
            return _error(hooks, "Usage: /transcript [limit]")
    return _info(hooks, "Transcript", formatTranscriptEntries(buildTranscriptSnapshot(session, limit)))


async def _handle_debug(
    arg_text: str,
    session: dict[str, Any],
    config: dict[str, Any],
    hooks: dict[str, Any],
    title: str = "Debug",
) -> dict[str, Any]:
    if not session.get("id"):
        return _error(hooks, "A persisted session id is required to create a debug bundle.")
    await _persist(hooks)
    try:
        report = await createDebugBundle(
            {
                "sessionId": session["id"],
                "cwd": session.get("cwd") or config.get("cwd") or os.getcwd(),
                "outputDir": arg_text.strip() or None,
                "session": session,
            }
        )
    except Exception as exc:
        return _error(hooks, str(exc))
    return _info(hooks, title, formatDebugBundleReport(report))


async def _handle_diagnostics(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if arg_text.strip():
        return _error(hooks, "Usage: /diagnostics")
    report = await collectDoctorReport(session.get("cwd") or config.get("cwd") or os.getcwd())
    return _info(hooks, "Diagnostics", formatDiagnosticsReport(report))


async def _handle_collaboration(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if arg_text.strip():
        return _error(hooks, "Usage: /collaboration")
    return _info(hooks, "Collaboration", await buildCollaborationStatusReport(session))


def formatTranscriptEntries(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No transcript entries for this session."
    sections = []
    for entry in entries:
        header = f"{entry.get('timestamp', 'saved')}  {entry.get('tone')}  {entry.get('title')}"
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        details = []
        if payload:
            details.append(f"tool         {payload.get('toolName') or 'Tool'}")
            details.append(f"failed       {'yes' if payload.get('failed') else 'no'}")
        body = str(entry.get("body") or "")
        if body:
            details.append(_indent_block(body, "  "))
        sections.append("\n".join([header, *details]))
    return "\n\n".join(sections)


def formatDebugBundleReport(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    return "\n".join(
        [
            f"session       {report.get('sessionId')}",
            f"bundle        {report.get('bundleDir') or report.get('path')}",
            f"manifest      {report.get('manifestPath')}",
            f"created       {report.get('createdAt')}",
            f"files         {len(report.get('includedFiles') or report.get('files') or [])}",
            f"agents        {summary.get('childAgentCount', 0)}",
            f"monitors      {summary.get('monitorCount', 0)}",
            f"plans         {summary.get('planApprovalCount', 0)}",
            f"tasks         {summary.get('taskCount', 0)}",
            f"compaction    {'yes' if summary.get('hasCompaction') else 'no'}",
            f"remote        {'yes' if summary.get('hasRemote') else 'no'}",
            f"bridge        {'yes' if summary.get('hasBridge') else 'no'}",
            f"transcript    {'yes' if summary.get('hasTranscriptLog') else 'no'}",
        ]
    )


def formatDiagnosticsReport(report: dict[str, Any]) -> str:
    checks = report.get("checks") or []
    lines = [
        f"healthy       {'yes' if report.get('healthy') else 'no'}",
        f"cwd           {report.get('cwd')}",
        f"state root    {report.get('stateRoot')}",
        "",
    ]
    for check in checks:
        message = check.get("message") or check.get("summary")
        suggestion = check.get("suggestion") or check.get("remediation")
        lines.append(f"{str(check.get('id')).ljust(14)} {str(check.get('status')).ljust(5)} {message}")
        if suggestion:
            lines.append(f"{'':14}       {suggestion}")
    return "\n".join(lines).rstrip()


async def buildCollaborationStatusReport(session: dict[str, Any]) -> str:
    agents = [record for record in await listChildAgents() if _is_related_child_agent(session, record)]
    monitors = await listSessionMonitors(session.get("id"))
    tasks = await listSessionTaskRecords(session.get("id"))
    metadata = session.get("metadata", {})
    remote = await loadRemoteSession(metadata.get("remoteSessionId")) if metadata.get("remoteSessionId") else None
    bridge = await loadBridge(metadata.get("bridgeId")) if metadata.get("bridgeId") else None
    awaiting = [record for record in agents if record.get("status") in {"awaiting_plan_approval", "awaiting_input"}]
    failed = [record for record in [*agents, *tasks, *monitors] if record.get("status") in {"failed", "crashed", "orphaned"}]
    sections = [
        "\n".join(
            [
                f"session        {session.get('id')}",
                f"workers        {len([record for record in agents if _is_active_child_agent(record)])}/{len(agents)}",
                f"awaiting       {len(awaiting)}",
                f"tasks          {len([record for record in tasks if _is_active_task_record(record)])}/{len(tasks)}",
                f"monitors       {sum(1 for record in monitors if record.get('status') == 'active')}/{len(monitors)}",
                f"failures       {len(failed)}",
                f"remote         {_short_id(remote.get('id', '')) + ' ' + str(remote.get('status')) if remote else 'none'}",
                f"bridge         {_short_id(bridge.get('id', '')) + ' ' + str(bridge.get('status')) if bridge else 'none'}",
            ]
        ),
        buildBackgroundReport(session, agents, monitors, [task for task in tasks if task.get("kind") in {"background_command", "main_session"}], remote, bridge),
    ]
    return "\n\n".join(section for section in sections if section)


async def _handle_review(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    run_prompt = hooks.get("runPrompt") if isinstance(hooks, dict) else None
    if not callable(run_prompt):
        return _error(hooks, "/review is only available in an interactive runtime.")
    result = run_prompt({"prompt": buildReviewPrompt(arg_text), "agentName": "reviewer"})
    if hasattr(result, "__await__"):
        result = await result
    return _handled((result or {}).get("text") if isinstance(result, dict) else result or "")


def _handle_statusline(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    get_visible = hooks.get("getStatusLineVisible") if isinstance(hooks, dict) else None
    set_visible = hooks.get("setStatusLineVisible") if isinstance(hooks, dict) else None
    if not callable(get_visible) or not callable(set_visible):
        return _error(hooks, "/statusline is only available in the interactive TUI.")
    if not arg_text:
        return _info(hooks, "Statusline", f"Status line is {'on' if get_visible() else 'off'}.")
    if arg_text not in {"on", "off"}:
        return _error(hooks, "Usage: /statusline [on|off]")
    set_visible(arg_text == "on")
    return _info(hooks, "Statusline", f"Status line {arg_text}.")


def _handle_theme(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    get_theme = hooks.get("getTheme") if isinstance(hooks, dict) else None
    set_theme = hooks.get("setTheme") if isinstance(hooks, dict) else None
    if not callable(get_theme) or not callable(set_theme):
        return _error(hooks, "/theme is only available in the interactive TUI.")
    if not arg_text:
        return _info(hooks, "Theme", f"Current theme: {get_theme()}\nAvailable: {', '.join(AVAILABLE_TUI_THEMES)}")
    if arg_text == "next":
        current = get_theme()
        index = AVAILABLE_TUI_THEMES.index(current) if current in AVAILABLE_TUI_THEMES else 0
        theme = AVAILABLE_TUI_THEMES[(index + 1) % len(AVAILABLE_TUI_THEMES)]
    else:
        theme = "tess" if arg_text.strip().lower() == "claude" else arg_text.strip().lower()
        if theme not in AVAILABLE_TUI_THEMES:
            return _error(hooks, f"Unknown theme: {arg_text}. Expected: {', '.join(AVAILABLE_TUI_THEMES)}")
    set_theme(theme)
    return _info(hooks, "Theme", f"Theme set to {theme}")


def _handle_mouse(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    get_mode = hooks.get("getMouseMode") if isinstance(hooks, dict) else None
    set_mode = hooks.get("setMouseMode") if isinstance(hooks, dict) else None
    if not callable(get_mode) or not callable(set_mode):
        return _error(hooks, "/mouse is only available in the interactive TUI.")
    if not arg_text:
        current = get_mode()
        return _info(
            hooks,
            "Mouse",
            "Mouse capture is on. Wheel and clicks control the TUI."
            if current == "capture"
            else "Mouse capture is off. Native terminal selection is enabled.",
        )
    if arg_text not in {"on", "off", "toggle", "capture", "selection"}:
        return _error(hooks, "Usage: /mouse [on|off|toggle]")
    current = get_mode()
    next_mode = "selection" if arg_text == "toggle" and current == "capture" else "capture" if arg_text in {"toggle", "on", "capture"} else "selection"
    set_mode(next_mode)
    return _info(
        hooks,
        "Mouse",
        "Mouse capture on. Wheel and clicks control the TUI."
        if next_mode == "capture"
        else "Mouse capture off. Native terminal selection enabled.",
    )


def _handle_screen(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    get_mode = hooks.get("getScreenMode") if isinstance(hooks, dict) else None
    set_mode = hooks.get("setScreenMode") if isinstance(hooks, dict) else None
    if not callable(get_mode) or not callable(set_mode):
        return _error(hooks, "/screen is only available in the interactive TUI.")
    if not arg_text:
        return _info(hooks, "Screen", f"Screen mode is {get_mode()}.\nAvailable: auto, fullscreen, inline")
    if arg_text not in {"auto", "fullscreen", "inline"}:
        return _error(hooks, "Usage: /screen [auto|fullscreen|inline]")
    set_mode(arg_text)
    return _info(hooks, "Screen", f"Screen mode set to {arg_text}.")


def formatMonitorRecord(record: dict[str, Any]) -> str:
    lines = [
        f"{_short_id(record.get('id', ''))}  {record.get('status')}  task {_short_id(record.get('taskId', ''))}",
        f"goal         {truncate(collapseWhitespace(record.get('goal', '')), 160)}",
        f"created      {str(record.get('createdAt', ''))[:19]}",
        f"interval     {record.get('checkIntervalMs')}ms check - {record.get('heartbeatIntervalMs')}ms heartbeat",
    ]
    if record.get("lastAssistantUpdateAt"):
        lines.append(f"last update  {str(record.get('lastAssistantUpdateAt'))[:19]}")
    if record.get("lastObservedTaskStatus"):
        lines.append(f"task status  {record.get('lastObservedTaskStatus')}")
    if record.get("failureReason"):
        lines.append(f"reason       {truncate(collapseWhitespace(record.get('failureReason')), 160)}")
    return "\n".join(lines)


def formatSessionCandidate(session: dict[str, Any]) -> str:
    name = session.get("name") or _first_user_text(session) or "untitled"
    return f"{_short_id(session.get('id', ''))}  {session.get('updatedAt', '')[:19]}  {session.get('model', '')}  {truncate(collapseWhitespace(name), 90)}"


def buildReviewPrompt(arg_text: str) -> str:
    base = [
        "Review the current workspace and implementation context like a strict code reviewer.",
        "Focus on bugs, regressions, risky assumptions, behavioral mismatches, and missing tests.",
        "Return findings first with file references when possible.",
    ]
    if arg_text.strip():
        base.extend(["", f"Additional review scope: {arg_text.strip()}"])
    return "\n".join(base)


def _split_subcommand(arg_text: str) -> tuple[str, str]:
    parts = arg_text.split(None, 1)
    return (parts[0].lower(), parts[1].strip() if len(parts) > 1 else "") if parts else ("", "")


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def _resolve_task_supervisor(args: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, bool]:
    args = args or {}
    supervisor = args.get("daemon") or args.get("taskSupervisor")
    if supervisor:
        return supervisor, False
    supervisor = await connectTaskSupervisorDaemonClient()
    should_close = bool(isinstance(supervisor, dict) and supervisor.get("_daemonClient"))
    return supervisor, should_close


async def _close_task_supervisor_if_needed(supervisor: dict[str, Any] | None, should_close: bool) -> None:
    if not should_close or not isinstance(supervisor, dict):
        return
    close = supervisor.get("close")
    if callable(close):
        await _maybe_await(close())


def _call_value(hooks: dict[str, Any], name: str) -> Any:
    fn = hooks.get(name) if isinstance(hooks, dict) else None
    return fn() if callable(fn) else None


def _emit_runtime(hooks: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    fn = hooks.get("emitRuntimeEvent") if isinstance(hooks, dict) else None
    if callable(fn):
        fn(event, data)


def _resolve_monitor(monitors: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    return next((monitor for monitor in monitors if str(monitor.get("id", "")).lower() == normalized), None) or next((monitor for monitor in monitors if normalized in str(monitor.get("id", "")).lower()), None)


def _resolve_task_record(records: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    return next((record for record in records if str(record.get("id", "")).lower() == normalized), None) or next((record for record in records if normalized in str(record.get("id", "")).lower()), None)


def _session_matches(session: dict[str, Any], query: str) -> bool:
    normalized = query.strip().lower()
    values = [session.get("id"), session.get("name"), session.get("cwd"), session.get("model"), _first_user_text(session)]
    return any(normalized in str(value).lower() for value in values if value)


def _first_user_text(session: dict[str, Any]) -> str:
    for message in session.get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text = " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")
            if text.strip():
                return text
    return ""


def _short_id(value: str) -> str:
    return value[:12] if len(value) > 12 else value


def collapseWhitespace(value: Any) -> str:
    return str(value or "").replace("\n", " ").replace("\t", " ").strip()


def truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: max(0, limit - 1)]}..."


def _indent_block(value: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in value.splitlines())


async def buildPermissionsReport(session: dict[str, Any], config: dict[str, Any]) -> str:
    current = session.get("permissionMode") or config.get("permissionMode") or "default"
    metadata = session.get("metadata") or {}
    parent_session_id = metadata.get("parentSessionId") or metadata.get("leaderSessionId")
    lines = [f"current  {current}"]
    if parent_session_id:
        parent = await loadSession(parent_session_id)
        parent_mode = (parent or {}).get("permissionMode")
        if parent_mode:
            lines.append(f"inherited {parent_mode} from {_short_id(parent_session_id)}")
            if parent_mode != current:
                lines.append(f"effective {parent_mode} while running as an interactive child")
        else:
            lines.append(f"inherited unknown from {_short_id(parent_session_id)}")
    lines.append(f"modes    {', '.join(PERMISSION_MODE_ORDER)}")
    return "\n".join(lines)


async def _persist_shell_settings(
    config: dict[str, Any],
    session: dict[str, Any],
    updates: dict[str, Any],
    hooks: dict[str, Any],
) -> None:
    clean_updates = {key: value for key, value in updates.items() if value is not None}
    if not clean_updates:
        return
    cwd = session.get("cwd") or config.get("cwd") or os.getcwd()
    scope = config.get("settingsWriteScope") or config.get("settingsScope") or "project"
    if scope not in {"user", "project", "local"}:
        scope = "project"
    settings = await loadSettings(scope, cwd)
    await saveSettings(scope, cwd, {**settings, **clean_updates})
    persist_choice = hooks.get("persistConfigChoice") if isinstance(hooks, dict) else None
    if callable(persist_choice):
        outcome = persist_choice({"scope": scope, "cwd": cwd, "updates": clean_updates})
        if hasattr(outcome, "__await__"):
            await outcome


async def _handle_provider(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if not arg_text:
        provider = session.get("provider") or config.get("provider")
        return _info(
            hooks,
            "Provider",
            "\n".join(
                [
                    f"profile   {session.get('providerProfileName') or config.get('providerProfileName')}",
                    f"kind      {provider}",
                    f"mode      {config.get('openaiApiMode') if provider == 'openai' else 'messages'}",
                    f"base url  {(config.get('openaiBaseUrl') or 'default') if provider == 'openai' else 'default'}",
                ]
            ),
        )
    requested = normalizeProviderProfileName(arg_text)
    profiles = config.get("providerProfiles") or {}
    if not requested or requested not in profiles:
        return _error(hooks, f"Unknown provider profile: {arg_text}")
    profile = profiles[requested]
    session["providerProfileName"] = requested
    config["providerProfileName"] = requested
    config["providerProfile"] = profile
    session["provider"] = config["provider"] = config["providerKind"] = profile.get("kind")
    if profile.get("kind") == "openai":
        config["openaiApiMode"] = profile.get("apiMode") or config.get("openaiApiMode")
        config["openaiBaseUrl"] = profile.get("baseUrl")
        config.pop("anthropicBaseUrl", None)
    else:
        config["anthropicBaseUrl"] = profile.get("baseUrl")
        config.pop("openaiBaseUrl", None)
    if profile.get("defaultModel"):
        session["model"] = config["model"] = normalizeModelName(profile["defaultModel"], profile.get("kind"))
        session["effort"] = config["effort"] = normalizeEffortForModel(profile.get("kind"), session.get("model"), session.get("effort") or config.get("effort")) or getDefaultEffortForProvider(profile.get("kind"))
    await _persist_shell_settings(
        config,
        session,
        {
            "provider": requested,
            "model": session.get("model") or config.get("model"),
            "effortLevel": session.get("effort") or config.get("effort"),
        },
        hooks,
    )
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Provider", f"Provider profile set to {requested}")


async def _handle_model(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    provider = session.get("provider") or config.get("provider") or "anthropic"
    if not arg_text:
        lines = [
            f"profile   {session.get('providerProfileName') or config.get('providerProfileName')}",
            f"provider  {provider}",
            f"current   {session.get('model') or config.get('model')}",
            "",
        ]
        for name in listProviders():
            lines.append(f"{name}{'  (active)' if name == provider else ''}")
            lines.append(f"  {', '.join(listModelsForProvider(name))}")
        lines.extend(["", "tip       /model openai:gpt-5.4 or /model anthropic:claude-opus-4-6"])
        return _info(hooks, "Model", "\n".join(lines))
    selection_provider, model = _parse_model_selection(arg_text, provider)
    switched_provider = selection_provider != provider
    session["provider"] = config["provider"] = config["providerKind"] = selection_provider
    session["model"] = config["model"] = normalizeModelName(model, selection_provider)
    profile_name = resolveFallbackProviderProfileName(
        selection_provider,
        config.get("providerProfiles") or {},
        None if switched_provider else session.get("providerProfileName") or config.get("providerProfileName"),
    )
    session["providerProfileName"] = config["providerProfileName"] = profile_name
    profile = (config.get("providerProfiles") or {}).get(profile_name) or config.get("providerProfile") or {}
    config["providerProfile"] = profile
    if profile.get("kind") == "openai":
        config["openaiApiMode"] = profile.get("apiMode") or config.get("openaiApiMode")
        config["openaiBaseUrl"] = profile.get("baseUrl")
        config.pop("anthropicBaseUrl", None)
    elif profile:
        config["anthropicBaseUrl"] = profile.get("baseUrl")
        config.pop("openaiBaseUrl", None)
    next_effort = getDefaultEffortForProvider(selection_provider) if switched_provider else session.get("effort") or config.get("effort")
    session["effort"] = config["effort"] = normalizeEffortForModel(selection_provider, session["model"], next_effort) or getDefaultEffortForProvider(selection_provider)
    await _persist_shell_settings(
        config,
        session,
        {
            "provider": profile_name or selection_provider,
            "model": session.get("model"),
            "effortLevel": session.get("effort"),
        },
        hooks,
    )
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Model", f"Model set to {selection_provider}:{session['model']}")


async def _handle_effort(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if not arg_text:
        return _info(hooks, "Effort", f"current  {session.get('effort') or config.get('effort')}\nlevels   low, medium, high, xhigh, max")
    effort = normalizeEffortLevel(arg_text)
    if not effort:
        return _error(hooks, f"Unknown effort level: {arg_text}. Expected one of: low, medium, high, xhigh, max")
    provider = session.get("provider") or config.get("provider") or "anthropic"
    model = session.get("model") or config.get("model") or ""
    if not modelSupportsEffort(provider, model):
        return _error(hooks, f"The active model does not support configurable reasoning effort: {model}")
    normalized = normalizeEffortForModel(provider, model, effort)
    if not normalized:
        return _error(hooks, f"Effort {effort} is not supported for {provider}:{model}")
    session["effort"] = config["effort"] = normalized
    await _persist_shell_settings(config, session, {"effortLevel": normalized}, hooks)
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Effort", f"Effort set to {normalized}")


async def _handle_permissions(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if not arg_text:
        return _info(hooks, "Permissions", await buildPermissionsReport(session, config))
    if arg_text.split(None, 1)[0] in {"policy", "allow", "deny", "reset"}:
        return await _handle_auto_mode_policy(arg_text, session, config, hooks)
    if arg_text not in PERMISSION_MODES:
        return _error(hooks, f"Unknown permission mode: {arg_text}. Expected one of: {', '.join(PERMISSION_MODE_ORDER)}")
    if (session.get("metadata") or {}).get("gitRepoPlanOnly") and arg_text != "plan":
        return _error(hooks, "Git repository not initialized. Only plan mode is available until you run `git init`.")
    session["permissionMode"] = config["permissionMode"] = arg_text
    await _persist_shell_settings(config, session, {"permissionMode": arg_text}, hooks)
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Permissions", f"Permission mode set to {arg_text}")


async def _handle_auto_mode_policy(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    cwd = session.get("cwd") or config.get("cwd") or os.getcwd()
    parts = arg_text.split(None, 2)
    command = parts[0]
    policy = await loadAutoMode(cwd)
    if command == "policy":
        return _info(hooks, "Permissions", formatAutoModePolicy(policy))
    if command == "reset":
        policy = getDefaultAutoMode()
        await saveAutoMode(cwd, policy)
        await _persist(hooks)
        return _info(hooks, "Permissions", "Auto-mode policy reset.\n\n" + formatAutoModePolicy(policy))
    if len(parts) < 3 or parts[1] not in {"add", "remove"}:
        return _error(hooks, "Usage: /permissions [policy|allow add TEXT|allow remove TEXT|deny add TEXT|deny remove TEXT|reset]")
    bucket = command
    action = parts[1]
    value = parts[2].strip()
    if not value:
        return _error(hooks, "Policy entry cannot be empty.")
    entries = [str(item) for item in policy.get(bucket, []) if str(item).strip()]
    if action == "add" and value not in entries:
        entries.append(value)
    if action == "remove":
        entries = [item for item in entries if item != value]
    policy[bucket] = entries
    await saveAutoMode(cwd, policy)
    await _persist(hooks)
    verb = "Added" if action == "add" else "Removed"
    return _info(hooks, "Permissions", f"{verb} auto-mode {bucket} entry: {value}\n\n{formatAutoModePolicy(policy)}")


def formatAutoModePolicy(policy: dict[str, Any]) -> str:
    allow = policy.get("allow") or []
    deny = policy.get("deny") or []
    environment = policy.get("environment") or {}
    return "\n".join(
        [
            "auto allow",
            *[f"  - {item}" for item in allow[:12]],
            "auto deny",
            *[f"  - {item}" for item in deny[:12]],
            f"auto env    {len(environment)} entries",
        ]
    )


def compactModelName(model: str) -> str:
    return model.replace("claude-", "").replace("-2025-08-07", "")


def normalizeModel(inputModel: str) -> str:
    return normalizeModelName(inputModel)


def formatUsageSummary(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "no tracked usage"
    return f"{usage.get('inputTokens', 0)} in · {usage.get('outputTokens', 0)} out · {usage.get('toolCalls', 0)} tools · {usage.get('turns', 0)} turns"


def formatUsageBreakdown(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "No tracked usage for this session yet."
    return "\n".join(
        [
            f"turns                    {usage.get('turns', 0)}",
            f"tool calls               {usage.get('toolCalls', 0)}",
            f"input tokens             {usage.get('inputTokens', 0)}",
            f"output tokens            {usage.get('outputTokens', 0)}",
            f"cache creation tokens    {usage.get('cacheCreationInputTokens', 0)}",
            f"cache read tokens        {usage.get('cacheReadInputTokens', 0)}",
        ]
    )


def formatPreflightTokenSummary(session: dict[str, Any]) -> str:
    measurement = ((session.get("metadata") or {}).get("tokenTracking") or {}).get("lastPreflight")
    if not isinstance(measurement, dict):
        return "n/a"
    margin = f" (+{measurement.get('safetyMarginTokens')})" if measurement.get("safetyMarginTokens") else ""
    return f"{measurement.get('inputTokens')}{margin} via {measurement.get('source')}"


def buildTodosReport(session: dict[str, Any]) -> str:
    todos = session.get("todos") or []
    return "No todos." if not todos else "\n".join(f"- [{todo.get('status', 'pending')}] {todo.get('content') or todo.get('task') or todo}" for todo in todos)


def _parse_command(raw: str) -> tuple[str, str]:
    parts = raw[1:].split(None, 1)
    return (parts[0].lower(), parts[1].strip() if len(parts) > 1 else "")


def _parse_model_selection(value: str, fallback_provider: str) -> tuple[str, str]:
    raw = value.strip()
    provider_only = normalizeProviderName(raw)
    if provider_only:
        return provider_only, getDefaultModelForProvider(provider_only)
    if ":" in raw:
        prefix, model = raw.split(":", 1)
        explicit_model = model.strip()
        provider = resolveProviderForModelSelection(normalizeProviderName(prefix), explicit_model) or fallback_provider
        return provider, explicit_model or getDefaultModelForProvider(provider)
    provider = resolveProviderForModelSelection(None, raw) or fallback_provider
    return provider, raw or getDefaultModelForProvider(provider)


def _sync_effective_provider(session: dict[str, Any], config: dict[str, Any]) -> None:
    model = session.get("model") or config.get("model")
    effective_provider = ensureProviderForModelSelection(
        session.get("provider") or config.get("provider"),
        model,
        config.get("provider") or "anthropic",
    )
    if effective_provider != session.get("provider"):
        session["provider"] = effective_provider
    if effective_provider != config.get("provider"):
        config["provider"] = effective_provider
        config["providerKind"] = effective_provider
    profile_name = resolveFallbackProviderProfileName(
        effective_provider,
        config.get("providerProfiles") or {},
        session.get("providerProfileName") or config.get("providerProfileName"),
    )
    session["providerProfileName"] = profile_name
    config["providerProfileName"] = profile_name


def _find_runtime_command(name: str, commands: list[Any]) -> Any:
    for command in commands:
        if isinstance(command, dict) and command.get("name", "").lower() == name.lower():
            return command
    return None


def _handled(message: Any) -> dict[str, Any]:
    return {"outcome": "handled", "message": message}


def _info(hooks: dict[str, Any], title: str, message: str) -> dict[str, Any]:
    fn = hooks.get("info") if isinstance(hooks, dict) else None
    if callable(fn):
        fn(title, message)
    return {"outcome": "handled", "title": title, "message": message}


def _error(hooks: dict[str, Any], message: str) -> dict[str, Any]:
    fn = hooks.get("error") if isinstance(hooks, dict) else None
    if callable(fn):
        fn(message)
    return {"outcome": "handled", "isError": True, "message": message}


def _call(hooks: dict[str, Any], name: str) -> None:
    fn = hooks.get(name) if isinstance(hooks, dict) else None
    if callable(fn):
        fn()


async def _persist(hooks: dict[str, Any]) -> None:
    fn = hooks.get("persist") if isinstance(hooks, dict) else None
    if callable(fn):
        result = fn()
        if hasattr(result, "__await__"):
            await result
