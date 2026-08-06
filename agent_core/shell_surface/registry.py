from __future__ import annotations

from typing import Any


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
