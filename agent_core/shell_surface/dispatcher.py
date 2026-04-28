from __future__ import annotations

from .shared import *


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
