from __future__ import annotations

from typing import Any, Callable

from .collaboration import createCollaborationRuntime
from .daemon import ensureTaskSupervisorDaemon
from .mcp import createMcpRuntime
from .plugins import loadPluginRuntime
from .runtime_events import createRuntimeEvent
from .runtime_state import createInitialRuntimeState, createRuntimeStateStore
from .session_runtime import createSessionRuntimeController
from .tools import createBuiltinTools
from .workspace import createWorkspaceController


async def bootstrapRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    legacy_subscribers: list[Callable[[str, dict[str, Any]], Any]] = []
    event_subscribers: list[Callable[[dict[str, Any]], Any]] = []
    owned_closers = []
    session = args.get("session")
    config = args.get("config") or {}
    cwd = args.get("cwd") or config.get("cwd") or (session or {}).get("cwd")
    state_store = createRuntimeStateStore(createInitialRuntimeState(session) if session else None)
    session_runtime: dict[str, Any] | None = None

    def emit_runtime_event(event: str, data: dict[str, Any] | None = None):
        active_session = session_runtime["getActiveSession"]() if session_runtime else session
        typed_event = createRuntimeEvent({"event": event, "data": data or {}, "defaultSessionId": (active_session or {}).get("id")})
        state_store["dispatch"](typed_event)
        envelope = {"event": event, "data": data or {}, "typedEvent": typed_event}
        for fn in list(event_subscribers):
            fn(envelope)
        for fn in list(legacy_subscribers):
            fn(event, data or {})

    def subscribe(fn):
        legacy_subscribers.append(fn)
        return lambda: legacy_subscribers.remove(fn) if fn in legacy_subscribers else None

    def subscribe_events(fn):
        event_subscribers.append(fn)
        return lambda: event_subscribers.remove(fn) if fn in event_subscribers else None

    session_runtime = createSessionRuntimeController({"session": session, "config": config, "emitRuntimeEvent": emit_runtime_event}) if session else None
    workspace_controller = args.get("workspaceController")
    if workspace_controller is None and session:
        workspace_controller = createWorkspaceController({"session": session, "config": config})
        refresh = getattr(workspace_controller, "refresh", None)
        if refresh:
            result = refresh()
            if hasattr(result, "__await__"):
                await result

    plugins = await loadPluginRuntime(config)
    plugin_mcp_servers = plugins.get("mcpServers") or []
    mcp_runtime = args.get("mcpRuntime")
    mcp_servers = args.get("mcpConfig") or config.get("mcpConfig") or []
    if mcp_runtime is None and (mcp_servers or plugin_mcp_servers):
        mcp_runtime = await createMcpRuntime(
            {
                "cwd": cwd,
                "servers": [*mcp_servers, *plugin_mcp_servers],
                "sessionId": (session or {}).get("id") or args.get("sessionId"),
                "registeredChannels": args.get("registeredChannels") or config.get("channels") or [],
                "onRuntimeEvent": emit_runtime_event,
            }
        )
        owned_closers.append(mcp_runtime["close"])

    daemon = args.get("daemon") or args.get("daemonClient")
    if daemon is None and args.get("daemonEnabled"):
        daemon = await ensureTaskSupervisorDaemon({"cwd": cwd})

    collaboration_runtime = args.get("collaborationRuntime")
    if collaboration_runtime is None and session:
        collaboration_runtime = await createCollaborationRuntime({"session": session, "config": config, "emit": emit_runtime_event})
        owned_closers.append(collaboration_runtime["close"])

    tool_options = {**args, "mcpRuntime": mcp_runtime, "workspaceController": workspace_controller}
    tools = _dedupe_tools([*createBuiltinTools(tool_options), *(mcp_runtime["createToolDefinitions"]() if mcp_runtime else []), *(plugins.get("tools") or [])])
    slash_commands = _dedupe_slash_commands(plugins.get("commands") or [])

    def set_foreground_busy(busy: bool) -> None:
        if collaboration_runtime and collaboration_runtime.get("setForegroundBusy"):
            collaboration_runtime["setForegroundBusy"](busy)

    def set_active_session(next_session: dict[str, Any], next_config: dict[str, Any]) -> None:
        nonlocal session, config
        session = next_session
        config = next_config
        if session_runtime:
            session_runtime["setActiveSession"](next_session, next_config)
        if workspace_controller and hasattr(workspace_controller, "setSession"):
            workspace_controller.setSession(next_session)
        if collaboration_runtime and collaboration_runtime.get("setSession"):
            collaboration_runtime["setSession"](next_session, next_config)

    async def close():
        for closer in owned_closers:
            result = closer()
            if hasattr(result, "__await__"):
                await result
        if daemon and args.get("closeDaemonOnRuntimeClose") and daemon.get("close"):
            result = daemon["close"]()
            if hasattr(result, "__await__"):
                await result

    return {
        "tools": tools,
        "slashCommands": slash_commands,
        "mcpRuntime": mcp_runtime,
        "mcp": mcp_runtime,
        "daemon": daemon,
        "collaborationRuntime": collaboration_runtime,
        "collaboration": collaboration_runtime,
        "workspace": workspace_controller,
        "plugins": plugins,
        "emitEvent": emit_runtime_event,
        "emitRuntimeEvent": emit_runtime_event,
        "subscribe": subscribe,
        "subscribeEvents": subscribe_events,
        "subscribeState": state_store["subscribe"],
        "getState": state_store["getState"],
        "stateStore": state_store,
        "setForegroundBusy": set_foreground_busy,
        "getCollaborationSnapshot": lambda: state_store["getState"]().get("collaboration"),
        "setActiveSession": set_active_session,
        "startTurn": session_runtime["startTurn"] if session_runtime else lambda *_args, **_kwargs: None,
        "setTurnCancelling": session_runtime["setTurnCancelling"] if session_runtime else lambda: None,
        "finishTurn": session_runtime["finishTurn"] if session_runtime else lambda *_args, **_kwargs: None,
        "failTurn": session_runtime["failTurn"] if session_runtime else lambda _error: None,
        "close": close,
    }


def _dedupe_tools(tools: list[Any]) -> list[Any]:
    seen = set()
    out = []
    for tool in tools:
        name = getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(tool)
    return out


def _dedupe_slash_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for command in commands:
        name = str(command.get("name") or "").lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(command)
    return out
