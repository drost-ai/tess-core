from __future__ import annotations

import json
import os
from typing import Any

from ._utils import now_iso
from .compaction import runCompaction
from .config import (
    getDefaultEffortForProvider,
    getDefaultModelForProvider,
    listModelsForProvider,
    listProviderProfiles,
    listProviders,
    normalizeEffortForModel,
    normalizeEffortLevel,
    normalizeModelName,
    normalizeProviderName,
    normalizeProviderProfileName,
    resolveFallbackProviderProfileName,
)
from .durable_memory import synchronizeManualProjectMemory
from .daemon import ensureTaskSupervisorDaemon, stopTaskSupervisorDaemon
from .remote import formatBridgeTail, formatRemoteSessionCandidate
from .storage import (
    getProjectMemoryPath,
    getDefaultAutoMode,
    listChildAgents,
    listPlanApprovals,
    listRemoteSessions,
    listSessionMonitors,
    listSessionTaskRecords,
    listSessions,
    loadAutoMode,
    loadDaemonState,
    loadTaskRecord,
    saveAutoMode,
    saveMonitor,
)
from .transcript import buildTranscriptSnapshot


AVAILABLE_TUI_THEMES = ["tess", "copper", "ocean", "forest"]
PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"}


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
        ("daemon", "/daemon [restart|stop]", "Show or control local task-supervisor daemon state"),
        ("remote", "/remote", "Show remote, bridge, and platform session state"),
        ("tasks", "/tasks [inspect|log|result|attach|stop|retry <id>]", "Show and control runtime tasks"),
        ("background", "/background [inspect|attach] [id]", "Show background workers, monitors, and remote activity"),
        ("review", "/review [scope]", "Run a reviewer-style pass in the current session"),
        ("cost", "/cost", "Show tracked token/tool usage"),
        ("monitors", "/monitors", "List active and recent background monitors"),
        ("monitor", "/monitor <stop|resume> <id>", "Manage a specific background monitor"),
        ("resume", "/resume [query]", "List or switch recent sessions"),
        ("compact", "/compact", "Summarize older context and trim it"),
        ("bug", "/bug", "Show the feedback URL"),
        ("transcript", "/transcript", "Render transcript"),
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
        return _info(hooks, "Plans", await buildPlansReport(session, arg_text))
    if command == "tasks":
        return _info(hooks, "Tasks", await buildTasksReport(session, arg_text, args))
    if command in {"agents", "background"}:
        return _info(hooks, "Agents" if command == "agents" else "Background", await buildAgentsReport(session, arg_text))
    if command == "daemon":
        return await _handle_daemon(arg_text, session, config, hooks, args)
    if command == "remote":
        return _info(hooks, "Remote", await buildRemoteReport(session))
    if command == "monitors":
        return _info(hooks, "Monitors", await buildMonitorsReport(session))
    if command == "monitor":
        return await _handle_monitor(arg_text, session, hooks)
    if command == "resume":
        return await _handle_resume(arg_text, session, hooks)
    if command == "compact":
        return await _handle_compact(arg_text, session, config, hooks, args)
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
    if command == "transcript":
        entries = buildTranscriptSnapshot(session)
        return {"outcome": "handled", "entries": entries, "message": json.dumps(entries, indent=2)}
    if command == "bug":
        return _info(hooks, "Bug", "Open an issue with the current debug bundle and reproduction details.")
    if runtime_command:
        result = runtime_command["handler"]({"argText": arg_text, "args": arg_text, "session": session, "config": config, "hooks": hooks})
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, dict):
            return result
        if result in {"handled", "exit", "pass"}:
            return {"outcome": result}
        return _handled(result or "")
    return {"outcome": "pass"}


def buildSlashHelp(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    hooks = args.get("hooks") or {}
    lines = [
        f"{command['usage']:<28} {command['description']}"
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
    return "\n".join(lines)


def buildStatusReport(session: dict[str, Any], config: dict[str, Any]) -> str:
    metadata = session.get("metadata", {})
    provider = session.get("provider") or config.get("provider")
    return "\n".join(
        [
            f"session      {session.get('id', 'n/a')}",
            f"cwd          {session.get('cwd') or config.get('cwd') or 'n/a'}",
            f"profile      {session.get('providerProfileName') or config.get('providerProfileName') or 'n/a'}",
            f"provider     {provider or 'n/a'}",
            f"model        {session.get('model') or config.get('model') or 'n/a'}",
            f"mode         {config.get('openaiApiMode') if provider == 'openai' else 'messages'}",
            f"base url     {config.get('openaiBaseUrl') if provider == 'openai' else config.get('anthropicBaseUrl') or 'default'}",
            f"effort       {session.get('effort') or config.get('effort') or 'n/a'}",
            f"permissions  {session.get('permissionMode') or config.get('permissionMode') or 'n/a'}",
            f"messages     {len(session.get('messages') or [])}",
            f"todos        {len(session.get('todos') or [])}",
            f"monitors     {len(metadata.get('monitorIds') or [])}",
            f"usage        {formatUsageSummary(metadata.get('usage'))}",
            f"compactions  {metadata.get('compactions', 0)}",
            f"archived     {metadata.get('fullTranscriptLog', {}).get('archivedMessageCount', 0)}",
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
    plans = [plan for plan in await listPlanApprovals() if plan.get("sessionId") == session.get("id")]
    if status_filter:
        plans = [plan for plan in plans if plan.get("status") == status_filter]
    if not plans:
        return "No plan approvals for this session."
    return "\n".join(f"{plan.get('requestId')} {plan.get('status')} {plan.get('createdAt', '')[:19]}" for plan in plans[:12])


async def buildTasksReport(session: dict[str, Any], arg_text: str = "", args: dict[str, Any] | None = None) -> str:
    parts = arg_text.split()
    records = await listSessionTaskRecords(session.get("id"))
    record = _resolve_task_record(records, parts[1]) if len(parts) > 1 else None
    if parts and parts[0] in {"inspect", "result"} and len(parts) > 1:
        record = record or await loadTaskRecord(parts[1])
        return json.dumps(record or {"error": f"Task not found: {parts[1]}"}, indent=2)
    if parts and parts[0] == "log" and len(parts) > 1:
        supervisor = (args or {}).get("daemon") or (args or {}).get("taskSupervisor")
        if supervisor and supervisor.get("readTaskOutput"):
            output = await _maybe_await(supervisor["readTaskOutput"]((record or {}).get("id") or parts[1], 12000))
            return output.get("output") or ""
        return "Task log requires a task supervisor."
    if parts and parts[0] in {"stop", "retry"} and len(parts) > 1:
        if not record:
            return f"Task not found: {parts[1]}"
        supervisor = (args or {}).get("daemon") or (args or {}).get("taskSupervisor")
        if not supervisor:
            return "Task action requires a task supervisor."
        if parts[0] == "stop":
            stop_fn = supervisor.get("stopTask")
            if not callable(stop_fn):
                return "Task supervisor does not support stopTask."
            stopped = await _maybe_await(stop_fn(record["id"]))
            return f"Stopped {record['id']}" if stopped else f"Task could not be stopped: {record['id']}"
        retry_fn = supervisor.get("retryTask")
        if not callable(retry_fn):
            return "Task supervisor does not support retryTask."
        retried = await _maybe_await(retry_fn(record["id"]))
        return "\n".join([f"Retried {record['id']}", f"new task     {retried.get('taskId') or retried.get('id')}", f"output       {retried.get('outputFile')}"]) if isinstance(retried, dict) else f"Retried {record['id']}"
    if not records:
        return "No runtime tasks for this session."
    return "\n".join(f"{record.get('id')} {record.get('status')} {record.get('kind')} {record.get('title')}" for record in records[:12])


async def buildAgentsReport(session: dict[str, Any], arg_text: str = "") -> str:
    agents = [agent for agent in await listChildAgents() if agent.get("parentSessionId") == session.get("id") or agent.get("agentId") in (session.get("metadata", {}).get("childAgentIds") or [])]
    parts = arg_text.split()
    if parts and parts[0] == "running":
        agents = [agent for agent in agents if agent.get("status") in {"queued", "running", "backgrounded", "awaiting_input", "awaiting_plan_approval"}]
    if parts and parts[0] == "inspect" and len(parts) > 1:
        match = next((agent for agent in agents if agent.get("agentId", "").startswith(parts[1]) or agent.get("sessionId", "").startswith(parts[1])), None)
        return json.dumps(match or {"error": f"Agent not found: {parts[1]}"}, indent=2)
    if not agents:
        return "No child agents for this session."
    return "\n".join(f"{agent.get('agentId')} {agent.get('status')} {agent.get('name') or agent.get('agentType') or ''} {agent.get('lastSummary') or ''}" for agent in agents[:12])


async def buildDaemonReport() -> str:
    daemon = await loadDaemonState()
    if not daemon:
        return "Daemon not running."
    return "\n".join(f"{key:<13} {daemon.get(key)}" for key in ("phase", "pid", "socketPath", "supervisorId", "epoch", "attachedClients", "watchedSessions", "activeTasks", "updatedAt"))


async def _handle_daemon(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    command = arg_text.strip().lower()
    controller = args.get("daemonController") or {}
    if not command:
        return _info(hooks, "Daemon", await buildDaemonReport())
    if command == "stop":
        stop_fn = controller.get("stop") if isinstance(controller, dict) else None
        if callable(stop_fn):
            await _maybe_await(stop_fn())
        else:
            await stopTaskSupervisorDaemon()
        return _info(hooks, "Daemon", "Daemon stopped.")
    if command == "restart":
        restart_fn = controller.get("restart") if isinstance(controller, dict) else None
        if callable(restart_fn):
            await _maybe_await(restart_fn())
        else:
            await stopTaskSupervisorDaemon()
            await ensureTaskSupervisorDaemon({"cwd": session.get("cwd") or config.get("cwd") or os.getcwd()})
        return _info(hooks, "Daemon", "Daemon restarted.")
    return _error(hooks, "Usage: /daemon [restart|stop]")


async def buildRemoteReport(session: dict[str, Any]) -> str:
    metadata = session.get("metadata", {})
    remotes = await listRemoteSessions()
    active = [remote for remote in remotes if remote.get("id") == metadata.get("remoteSessionId") or remote.get("sessionId") == session.get("id")]
    if not active:
        return "No remote sessions linked to this session."
    lines = [formatRemoteSessionCandidate(remote) for remote in active[:8]]
    bridge_id = metadata.get("bridgeId")
    if bridge_id:
        lines.extend(["", "Bridge events:", await formatBridgeTail(bridge_id, 8)])
    return "\n".join(lines)


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
    if _call_value(hooks, "isCompacting"):
        return _info(hooks, "Compact", "Compaction is already running.")
    _emit_runtime(hooks, "compaction.started", {"session_id": session.get("id"), "trigger": "manual", "current_token_estimate": ((session.get("metadata") or {}).get("compactionState") or {}).get("workingContextTokenEstimate")})
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
        _emit_runtime(hooks, "compaction.failed", {"session_id": session.get("id"), "trigger": "manual", "error": str(exc)})
        return _error(hooks, str(exc))
    if outcome.get("status") == "skipped":
        _emit_runtime(hooks, "compaction.skipped", {"session_id": session.get("id"), "trigger": "manual", "reason": outcome.get("reason"), "detail": outcome.get("detail")})
        return _info(hooks, "Compact", "\n".join(["Checkpoint not created.", f"reason      {outcome.get('reason')}", f"detail      {outcome.get('detail')}"]))
    _call(hooks, "refresh")
    await _persist(hooks)
    _emit_runtime(hooks, "compaction.completed", {"session_id": session.get("id"), "trigger": outcome.get("trigger"), "generation": outcome.get("generation"), "compacted_through_message_index": outcome.get("compactedThroughMessageIndex")})
    artifacts = outcome.get("artifacts") or {}
    return _info(hooks, "Compact", "\n".join(["Compacted working context.", f"generation  {outcome.get('generation')}", f"summary     {artifacts.get('summary', 'n/a')}", f"checkpoint  {artifacts.get('checkpoint', 'n/a')}"]))


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
        return _info(hooks, "Mouse", f"Mouse mode is {get_mode()}.")
    if arg_text not in {"on", "off", "toggle", "capture", "selection"}:
        return _error(hooks, "Usage: /mouse [on|off|toggle]")
    current = get_mode()
    next_mode = "selection" if arg_text == "toggle" and current == "capture" else "capture" if arg_text in {"toggle", "on", "capture"} else "selection"
    set_mode(next_mode)
    return _info(hooks, "Mouse", f"Mouse mode set to {next_mode}.")


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


async def _handle_provider(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if not arg_text:
        return _info(hooks, "Provider", f"profile   {session.get('providerProfileName') or config.get('providerProfileName')}\nkind      {session.get('provider') or config.get('provider')}")
    requested = normalizeProviderProfileName(arg_text)
    profiles = config.get("providerProfiles") or {}
    if not requested or requested not in profiles:
        return _error(hooks, f"Unknown provider profile: {arg_text}")
    profile = profiles[requested]
    session["providerProfileName"] = requested
    config["providerProfileName"] = requested
    config["providerProfile"] = profile
    session["provider"] = config["provider"] = config["providerKind"] = profile.get("kind")
    if profile.get("defaultModel"):
        session["model"] = config["model"] = normalizeModelName(profile["defaultModel"], profile.get("kind"))
    session["effort"] = config["effort"] = normalizeEffortForModel(profile.get("kind"), session.get("model"), session.get("effort") or config.get("effort")) or getDefaultEffortForProvider(profile.get("kind"))
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Provider", f"Provider profile set to {requested}")


async def _handle_model(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    provider = session.get("provider") or config.get("provider") or "anthropic"
    if not arg_text:
        lines = [f"current   {session.get('model') or config.get('model')}"]
        for name in listProviders():
            lines.append(f"{name}{'  (active)' if name == provider else ''}")
            lines.append(f"  {', '.join(listModelsForProvider(name))}")
        return _info(hooks, "Model", "\n".join(lines))
    selection_provider, model = _parse_model_selection(arg_text, provider)
    session["provider"] = config["provider"] = config["providerKind"] = selection_provider
    session["model"] = config["model"] = normalizeModelName(model, selection_provider)
    profile_name = resolveFallbackProviderProfileName(selection_provider, config.get("providerProfiles") or {}, session.get("providerProfileName") or config.get("providerProfileName"))
    session["providerProfileName"] = config["providerProfileName"] = profile_name
    session["effort"] = config["effort"] = normalizeEffortForModel(selection_provider, session["model"], session.get("effort") or config.get("effort")) or getDefaultEffortForProvider(selection_provider)
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Model", f"Model set to {selection_provider}:{session['model']}")


async def _handle_effort(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if not arg_text:
        return _info(hooks, "Effort", f"current   {session.get('effort') or config.get('effort')}\nlevels    low, medium, high, xhigh, max")
    effort = normalizeEffortLevel(arg_text)
    if not effort:
        return _error(hooks, f"Unknown effort level: {arg_text}")
    provider = session.get("provider") or config.get("provider")
    normalized = normalizeEffortForModel(provider, session.get("model") or config.get("model"), effort)
    if not normalized:
        return _error(hooks, f"Effort {effort} is not supported by {session.get('model') or config.get('model')}")
    session["effort"] = config["effort"] = normalized
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Effort", f"Effort set to {normalized}")


async def _handle_permissions(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if not arg_text:
        policy = await loadAutoMode(session.get("cwd") or config.get("cwd") or os.getcwd())
        return _info(hooks, "Permissions", f"current   {session.get('permissionMode') or config.get('permissionMode')}\nlevels    {', '.join(sorted(PERMISSION_MODES))}\n\n{formatAutoModePolicy(policy)}")
    if arg_text.split(None, 1)[0] in {"policy", "allow", "deny", "reset"}:
        return await _handle_auto_mode_policy(arg_text, session, config, hooks)
    if arg_text not in PERMISSION_MODES:
        return _error(hooks, f"Unknown permission mode: {arg_text}")
    session["permissionMode"] = config["permissionMode"] = arg_text
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
        return "0 turns, 0 tools, 0 input, 0 output"
    return f"{usage.get('turns', 0)} turns, {usage.get('toolCalls', 0)} tools, {usage.get('inputTokens', 0)} input, {usage.get('outputTokens', 0)} output"


def formatUsageBreakdown(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "No usage recorded."
    return "\n".join(f"{key}: {value}" for key, value in usage.items())


def buildTodosReport(session: dict[str, Any]) -> str:
    todos = session.get("todos") or []
    return "No todos." if not todos else "\n".join(f"- [{todo.get('status', 'pending')}] {todo.get('content') or todo.get('task') or todo}" for todo in todos)


def _parse_command(raw: str) -> tuple[str, str]:
    parts = raw[1:].split(None, 1)
    return (parts[0].lower(), parts[1].strip() if len(parts) > 1 else "")


def _parse_model_selection(value: str, fallback_provider: str) -> tuple[str, str]:
    if ":" in value:
        prefix, model = value.split(":", 1)
        provider = normalizeProviderName(prefix) or fallback_provider
        return provider, model.strip()
    return fallback_provider, value.strip()


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
