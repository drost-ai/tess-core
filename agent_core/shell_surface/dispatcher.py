from __future__ import annotations

from .agents import _handle_agents
from .common import (
    _call,
    _error,
    _handled,
    _info,
)
from .context_commands import (
    _handle_compact,
    _handle_extensions,
    _handle_memory,
    _handle_monitor,
    _handle_resume,
    build_monitors_report,
)
from .diagnostics import (
    _handle_collaboration,
    _handle_debug,
    _handle_diagnostics,
    _handle_mouse,
    _handle_review,
    _handle_screen,
    _handle_statusline,
    _handle_theme,
    _handle_transcript,
)
from .registry import build_slash_help
from .remote_background import _handle_background, _handle_daemon, _handle_remote
from .status import (
    _find_runtime_command,
    _handle_effort,
    _handle_model,
    _handle_permissions,
    _handle_provider,
    _parse_command,
    _sync_effective_provider,
    build_provider_profiles_report,
    build_status_report,
    build_todos_report,
    format_usage_breakdown,
)
from .tasks import _handle_tasks
from typing import Any, Callable


def _clear(ctx: dict[str, Any]) -> dict[str, Any]:
    _call(ctx["hooks"], "clear")
    return _handled("cleared")


async def _monitors(ctx: dict[str, Any]) -> dict[str, Any]:
    return _info(ctx["hooks"], "Monitors", await build_monitors_report(ctx["session"]))


# One handler per builtin command listed in registry._COMMANDS (a test keeps
# the two tables in lockstep). Each handler takes the dispatch context and
# returns a result dict or an awaitable of one; builtins shadow runtime
# commands of the same name.
_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "help": lambda c: _info(c["hooks"], "Commands", build_slash_help({"hooks": c["hooks"], "runtimeCommands": c["runtimeCommands"]})),
    "exit": lambda c: {"outcome": "exit"},
    "quit": lambda c: {"outcome": "exit"},
    "clear": _clear,
    "status": lambda c: _info(c["hooks"], "Status", build_status_report(c["session"], c["config"])),
    "providers": lambda c: _info(c["hooks"], "Providers", build_provider_profiles_report(c["session"], c["config"])),
    "provider": lambda c: _handle_provider(c["argText"], c["session"], c["config"], c["hooks"]),
    "model": lambda c: _handle_model(c["argText"], c["session"], c["config"], c["hooks"]),
    "effort": lambda c: _handle_effort(c["argText"], c["session"], c["config"], c["hooks"]),
    "permissions": lambda c: _handle_permissions(c["argText"], c["session"], c["config"], c["hooks"]),
    "todos": lambda c: _info(c["hooks"], "Todos", build_todos_report(c["session"])),
    "memory": lambda c: _handle_memory(c["argText"], c["session"], c["hooks"]),
    "extensions": lambda c: _handle_extensions(c["hooks"]),
    "cost": lambda c: _info(c["hooks"], "Usage", format_usage_breakdown(c["session"].get("metadata", {}).get("usage"))),
    "tasks": lambda c: _handle_tasks(c["argText"], c["session"], c["hooks"], c["request"]),
    "agents": lambda c: _handle_agents(c["argText"], c["session"], c["config"], c["hooks"], c["request"]),
    "background": lambda c: _handle_background(c["argText"], c["session"], c["hooks"]),
    "daemon": lambda c: _handle_daemon(c["argText"], c["session"], c["config"], c["hooks"], c["request"]),
    "remote": lambda c: _handle_remote(c["argText"], c["session"], c["hooks"]),
    "monitors": _monitors,
    "monitor": lambda c: _handle_monitor(c["argText"], c["session"], c["hooks"]),
    "resume": lambda c: _handle_resume(c["argText"], c["session"], c["hooks"]),
    "compact": lambda c: _handle_compact(c["argText"], c["session"], c["config"], c["hooks"], c["request"]),
    "transcript": lambda c: _handle_transcript(c["argText"], c["session"], c["hooks"]),
    "debug": lambda c: _handle_debug(c["argText"], c["session"], c["config"], c["hooks"]),
    "diagnostics": lambda c: _handle_diagnostics(c["argText"], c["session"], c["config"], c["hooks"]),
    "collaboration": lambda c: _handle_collaboration(c["argText"], c["session"], c["hooks"]),
    "review": lambda c: _handle_review(c["argText"], c["hooks"]),
    "statusline": lambda c: _handle_statusline(c["argText"], c["hooks"]),
    "theme": lambda c: _handle_theme(c["argText"], c["hooks"]),
    "mouse": lambda c: _handle_mouse(c["argText"], c["hooks"]),
    "screen": lambda c: _handle_screen(c["argText"], c["hooks"]),
    "bug": lambda c: _info(c["hooks"], "Feedback", "Report issues at https://github.com/drost-ai/tess-core/issues"),
}


async def handle_slash_command(args: dict[str, Any] | str | None = None, **kwargs: Any) -> dict[str, Any]:
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
    handler = _HANDLERS.get(command)
    if handler:
        result = handler(
            {
                "argText": arg_text,
                "session": session,
                "config": config,
                "hooks": hooks,
                "runtimeCommands": runtime_commands,
                "request": args,
            }
        )
        if hasattr(result, "__await__"):
            result = await result
        return result
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
