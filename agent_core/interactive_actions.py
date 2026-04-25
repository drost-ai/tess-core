from __future__ import annotations

import json
from typing import Any

from .config import (
    getDefaultEffortForProvider,
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
from .remote import formatBridgeTail, formatRemoteSessionCandidate
from .storage import (
    listChildAgents,
    listPlanApprovals,
    listRemoteSessions,
    listSessionTaskRecords,
    loadDaemonState,
    loadTaskRecord,
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
    if runtime_command:
        result = runtime_command["handler"]({"args": arg_text, "session": session, "config": config, "hooks": hooks})
        if hasattr(result, "__await__"):
            result = await result
        return _handled(result or "")

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
    if command == "cost":
        return _info(hooks, "Usage", formatUsageBreakdown(session.get("metadata", {}).get("usage")))
    if command == "plans":
        return _info(hooks, "Plans", await buildPlansReport(session, arg_text))
    if command == "tasks":
        return _info(hooks, "Tasks", await buildTasksReport(session, arg_text, args))
    if command in {"agents", "background"}:
        return _info(hooks, "Agents" if command == "agents" else "Background", await buildAgentsReport(session, arg_text))
    if command == "daemon":
        return _info(hooks, "Daemon", await buildDaemonReport())
    if command == "remote":
        return _info(hooks, "Remote", await buildRemoteReport(session))
    if command == "transcript":
        entries = buildTranscriptSnapshot(session)
        return {"outcome": "handled", "entries": entries, "message": json.dumps(entries, indent=2)}
    if command == "bug":
        return _info(hooks, "Bug", "Open an issue with the current debug bundle and reproduction details.")
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
    lines = []
    for name in listProviderProfiles(profiles):
        profile = profiles.get(name, {})
        marker = "*" if name == active else " "
        lines.append(f"{marker} {name:<24} {profile.get('kind', ''):<10} {profile.get('defaultModel') or ''}")
    return "\n".join(lines) if lines else "No provider profiles configured."


async def buildPlansReport(session: dict[str, Any], status_filter: str = "") -> str:
    plans = [plan for plan in await listPlanApprovals() if plan.get("sessionId") == session.get("id")]
    if status_filter:
        plans = [plan for plan in plans if plan.get("status") == status_filter]
    if not plans:
        return "No plan approvals for this session."
    return "\n".join(f"{plan.get('requestId')} {plan.get('status')} {plan.get('createdAt', '')[:19]}" for plan in plans[:12])


async def buildTasksReport(session: dict[str, Any], arg_text: str = "", args: dict[str, Any] | None = None) -> str:
    parts = arg_text.split()
    if parts and parts[0] in {"inspect", "result"} and len(parts) > 1:
        record = await loadTaskRecord(parts[1])
        return json.dumps(record or {"error": f"Task not found: {parts[1]}"}, indent=2)
    if parts and parts[0] == "log" and len(parts) > 1:
        supervisor = (args or {}).get("daemon") or (args or {}).get("taskSupervisor")
        if supervisor and supervisor.get("readTaskOutput"):
            output = await supervisor["readTaskOutput"](parts[1], 12000)
            return output.get("output") or ""
        return "Task log requires a task supervisor."
    records = await listSessionTaskRecords(session.get("id"))
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
        return _info(hooks, "Permissions", f"current   {session.get('permissionMode') or config.get('permissionMode')}\nlevels    {', '.join(sorted(PERMISSION_MODES))}")
    if arg_text not in PERMISSION_MODES:
        return _error(hooks, f"Unknown permission mode: {arg_text}")
    session["permissionMode"] = config["permissionMode"] = arg_text
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Permissions", f"Permission mode set to {arg_text}")


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
