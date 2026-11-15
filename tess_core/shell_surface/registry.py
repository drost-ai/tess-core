from __future__ import annotations

from typing import Any

# Single source of truth for the builtin slash commands. The dispatcher's
# handler table must cover exactly these names (a test enforces it), the
# help text derives from this list, and availability gating reads the
# metadata tables below instead of re-declaring name sets.
_COMMANDS: list[tuple[str, str, str]] = [
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
    ("statusline", "/statusline [on|off]", "Toggle the footer line"),
    ("theme", "/theme [name|next]", "Switch the TUI color theme"),
    ("mouse", "/mouse [on|off|toggle]", "Toggle mouse capture vs native terminal selection"),
    ("screen", "/screen [auto|fullscreen|inline]", "Switch between inline scrollback and fullscreen transcript rendering"),
]

# Subcommand help rendered directly beneath the parent command in /help.
_SUBHELP: dict[str, list[tuple[str, str]]] = {
    "agents": [
        ("/agents running", "Show active child agents for this session"),
        ("/agents all", "Show all child agents for this session"),
        ("/agents inspect ID", "Inspect a child agent"),
        ("/agents attach ID", "Attach to a child agent session"),
        ("/agents stop ID", "Stop a daemon-backed child agent"),
        ("/agents retry ID", "Retry a daemon-backed child agent"),
    ],
    "memory": [
        ("/memory append TEXT", "Append text to project memory"),
        ("/memory set TEXT", "Replace project memory contents"),
        ("/memory clear", "Delete project memory"),
    ],
    "tasks": [
        ("/tasks log ID", "Show recent task output"),
        ("/tasks result ID", "Show the latest task result surface"),
        ("/tasks attach ID", "Reconnect to an attachable task session"),
        ("/tasks stop ID", "Stop a daemon-backed task"),
        ("/tasks retry ID", "Retry a retryable task"),
    ],
    "remote": [
        ("/remote list", "List known remote sessions"),
        ("/remote tail ID", "Show recent bridge events"),
        ("/remote queue TEXT", "Queue bridge input for the active remote"),
        ("/remote close ID", "Close a remote bridge/session record"),
    ],
    "compact": [
        ("/compact status", "Show checkpoint and artifact state"),
        ("/compact debug", "Create a debug bundle with compaction artifacts"),
    ],
}

# TUI toggle commands: listed in /help only when both hooks are wired, and
# usable only in interactive screen modes.
_TOGGLE_HOOKS: dict[str, tuple[str, str]] = {
    "statusline": ("getStatusLineVisible", "setStatusLineVisible"),
    "theme": ("getTheme", "setTheme"),
    "mouse": ("getMouseMode", "setMouseMode"),
    "screen": ("getScreenMode", "setScreenMode"),
}

# Commands that are always listed but need a hook to actually run.
_HOOK_REQUIREMENTS: dict[str, tuple[str, str]] = {
    "review": ("runPrompt", "interactive runtime required"),
    "extensions": ("getExtensionsReport", "extension report unavailable"),
}


def list_slash_commands(args: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The available slash commands with their usage and description, including those the runtime adds."""
    args = dict(args or {})
    commands = [
        entry for entry in _COMMANDS
        if entry[0] not in _TOGGLE_HOOKS or args.get(f"include{entry[0].capitalize()}")
    ]
    for command in args.get("runtimeCommands") or []:
        name = command.get("name") if isinstance(command, dict) else getattr(command, "name", None)
        if not name:
            continue
        usage = command.get("usage") if isinstance(command, dict) else getattr(command, "usage", None)
        description = command.get("description") if isinstance(command, dict) else getattr(command, "description", "")
        commands.append((name, usage or f"/{name}", description))
    return [{"name": name, "usage": usage, "description": description} for name, usage, description in commands]


def get_slash_command_availability(args: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    args = dict(args or {})
    hooks = args.get("hooks") or {}
    mode = str(args.get("mode") or ("bare" if args.get("bare") else "tui")).lower()
    commands = list_slash_commands(
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
        available = True
        reason = None
        if command["name"] in _TOGGLE_HOOKS and mode not in interactive_modes:
            available = False
            reason = "full-screen TUI required"
        requirement = _HOOK_REQUIREMENTS.get(command["name"])
        if requirement and not callable(hooks.get(requirement[0])):
            available = False
            reason = requirement[1]
        availability.append({**command, "mode": mode, "available": available, "reason": reason})
    return availability


def build_slash_help(args: dict[str, Any] | None = None) -> str:
    args = dict(args or {})
    hooks = args.get("hooks") or {}
    include_flags = {
        f"include{name.capitalize()}": bool(all(hooks.get(hook) for hook in hook_names))
        for name, hook_names in _TOGGLE_HOOKS.items()
    }
    lines = []
    for command in list_slash_commands({**include_flags, "runtimeCommands": args.get("runtimeCommands") or []}):
        lines.append(f"{command['usage']:<22} {command['description']}")
        for usage, description in _SUBHELP.get(command["name"], ()):
            lines.append(f"{usage:<22} {description}")
    return "\n".join(lines)
