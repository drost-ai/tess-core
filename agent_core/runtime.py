from __future__ import annotations

from typing import Any, Callable

from ._utils import maybe_await
from .collaboration import createCollaborationRuntime
from .daemon import ensureTaskSupervisorDaemon
from .mcp import createMcpRuntime
from .monitors import createMonitorRuntime
from .plugins import loadPluginRuntime
from .remote import createBridgeRuntime
from .runtime_events import createRuntimeEvent
from .runtime_state import createInitialRuntimeState, createRuntimeStateStore
from .session_runtime import createSessionRuntimeController
from .storage import configureRuntimeHome
from .task_runtime import createTaskRuntime
from .tools import createBuiltinTools
from .workspace import createWorkspaceController
from .config import getDefaultToolList


async def bootstrapRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    legacy_subscribers: list[Callable[[str, dict[str, Any]], Any]] = []
    event_subscribers: list[Callable[[dict[str, Any]], Any]] = []
    session = args.get("session")
    config = args.get("config") or {}
    if config.get("runtimeHome"):
        configureRuntimeHome(config["runtimeHome"])
    cwd = args.get("cwd") or config.get("cwd") or (session or {}).get("cwd")
    state_store = createRuntimeStateStore(createInitialRuntimeState(session) if session else None)
    session_runtime: dict[str, Any] | None = None
    daemon_unsubscribe = None
    owned_mcp_close = None
    owned_bridge_close = None
    owned_monitor_close = None
    owned_task_close = None
    owned_collaboration_close = None

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
        owned_mcp_close = mcp_runtime["close"]

    daemon = args.get("daemon") or args.get("daemonClient")
    if daemon is None and args.get("daemonEnabled"):
        daemon = await ensureTaskSupervisorDaemon({"cwd": cwd})

    bridge_runtime = args.get("bridgeRuntime") or args.get("bridge")
    session_metadata = (session or {}).get("metadata") if isinstance((session or {}).get("metadata"), dict) else {}
    if bridge_runtime is None and session and (args.get("enableBridge") or session_metadata.get("remoteControlEnabled")):
        bridge_runtime = await createBridgeRuntime(
            {
                "session": session,
                "remoteSessionId": session_metadata.get("remoteSessionId"),
                "name": args.get("bridgeName"),
                "emit": emit_runtime_event,
                **({"startPolling": args["bridgeStartPolling"]} if "bridgeStartPolling" in args else {}),
            }
        )
        owned_bridge_close = bridge_runtime["close"]

    monitor_runtime = args.get("monitorRuntime") or args.get("monitors")
    if monitor_runtime is None and session and not daemon:
        monitor_runtime = await createMonitorRuntime({"session": session, "config": config, "emit": emit_runtime_event})
        owned_monitor_close = monitor_runtime["close"]

    task_runtime = args.get("taskRuntime") or args.get("tasks")
    if task_runtime is None and session and not daemon:
        task_runtime = await createTaskRuntime({"session": session, "config": config, "emit": emit_runtime_event})
        owned_task_close = task_runtime["close"]

    collaboration_runtime = args.get("collaborationRuntime")
    if collaboration_runtime is None and session and not daemon:
        collaboration_runtime = await createCollaborationRuntime({"session": session, "config": config, "emit": emit_runtime_event})
        owned_collaboration_close = collaboration_runtime["close"]

    if daemon and session and daemon.get("subscribe"):
        daemon_unsubscribe = await maybe_await(daemon["subscribe"](session["id"], lambda record: emit_runtime_event(record.get("event"), record.get("data") or {})))
        if daemon.get("getStatus"):
            daemon_status = await _call_runtime_method(daemon, "getStatus")
            if daemon_status:
                emit_runtime_event("daemon.snapshot", _daemon_snapshot_payload(daemon_status))

    tool_options = {**args, "mcpRuntime": mcp_runtime, "workspaceController": workspace_controller}
    tools = _dedupe_tools([*createBuiltinTools(tool_options), *(mcp_runtime["createToolDefinitions"]() if mcp_runtime else []), *(plugins.get("tools") or [])])
    slash_commands = _dedupe_slash_commands(plugins.get("commands") or [])
    _augment_config_tools(config, [_tool_name(tool) for tool in tools if _tool_name(tool)])

    def set_foreground_busy(busy: bool) -> None:
        if monitor_runtime and monitor_runtime.get("setForegroundBusy"):
            monitor_runtime["setForegroundBusy"](busy)
        if task_runtime and task_runtime.get("setForegroundBusy"):
            task_runtime["setForegroundBusy"](busy)
        if collaboration_runtime and collaboration_runtime.get("setForegroundBusy"):
            collaboration_runtime["setForegroundBusy"](busy)

    def set_active_session(next_session: dict[str, Any], next_config: dict[str, Any]) -> None:
        nonlocal session, config, daemon_unsubscribe
        session = next_session
        config = next_config
        if session_runtime:
            session_runtime["setActiveSession"](next_session, next_config)
        if workspace_controller and hasattr(workspace_controller, "setSession"):
            workspace_controller.setSession(next_session)
        if monitor_runtime and monitor_runtime.get("setSession"):
            monitor_runtime["setSession"](next_session, next_config)
        if task_runtime and task_runtime.get("setSession"):
            task_runtime["setSession"](next_session, next_config)
        if collaboration_runtime and collaboration_runtime.get("setSession"):
            collaboration_runtime["setSession"](next_session, next_config)
        if daemon and daemon.get("subscribe"):
            if daemon_unsubscribe:
                daemon_unsubscribe()
            async def subscribe_next() -> None:
                nonlocal daemon_unsubscribe
                daemon_unsubscribe = await maybe_await(daemon["subscribe"](next_session["id"], lambda record: emit_runtime_event(record.get("event"), record.get("data") or {})))
            _run_background(subscribe_next())

    async def close():
        if daemon_unsubscribe:
            daemon_unsubscribe()
        if daemon and daemon.get("close"):
            await maybe_await(daemon["close"]())
        for closer in (owned_collaboration_close, owned_task_close, owned_monitor_close, owned_bridge_close, owned_mcp_close):
            if not closer:
                continue
            result = closer()
            if hasattr(result, "__await__"):
                await result

    return {
        "tools": tools,
        "slashCommands": slash_commands,
        "mcpRuntime": mcp_runtime,
        "mcp": mcp_runtime,
        "daemon": daemon,
        "monitorRuntime": monitor_runtime,
        "monitors": monitor_runtime,
        "taskRuntime": task_runtime,
        "tasks": task_runtime,
        "collaborationRuntime": collaboration_runtime,
        "collaboration": collaboration_runtime,
        "bridgeRuntime": bridge_runtime,
        "bridge": bridge_runtime,
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
        "getMonitorSnapshot": lambda: state_store["getState"]().get("monitors", {}).get("snapshot"),
        "getTaskSnapshot": lambda: task_runtime["getSnapshot"]() if task_runtime and task_runtime.get("getSnapshot") else _task_snapshot_from_state(state_store["getState"]),
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


def _tool_name(tool: Any) -> str | None:
    return getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)


def _augment_config_tools(config: dict[str, Any], runtime_tool_names: list[str]) -> None:
    tools = config.setdefault("tools", [])
    allowed = config.setdefault("allowedTools", [])
    default_tools = set(getDefaultToolList())
    default_allow_list = len(tools) == len(default_tools) and len(allowed) == len(default_tools) and all(tool in default_tools for tool in tools) and all(tool in default_tools for tool in allowed)
    for name in runtime_tool_names:
        if name not in tools:
            tools.append(name)
        if default_allow_list and name not in allowed:
            allowed.append(name)


def _task_snapshot_from_state(get_state: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    tasks = get_state().get("tasks", {})
    return {
        "total": tasks.get("total", 0),
        "active": tasks.get("activeCount", 0),
        "backgrounded": tasks.get("backgroundedCount", 0),
        "awaitingInput": tasks.get("awaitingInputCount", 0),
        "failed": tasks.get("failedCount", 0),
        "labels": tasks.get("labels", []),
        "tasks": tasks.get("tasks", []),
    }


def _daemon_snapshot_payload(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": _first_present(status, "phase"),
        "epoch": _first_present(status, "epoch"),
        "pid": _first_present(status, "pid"),
        "socket_path": _first_present(status, "socketPath", "socket_path"),
        "supervisor_id": _first_present(status, "supervisorId", "supervisor_id"),
        "attached_clients": _first_present(status, "attachedClients", "attached_clients"),
        "watched_sessions": _first_present(status, "watchedSessions", "watched_sessions"),
        "active_tasks": _first_present(status, "activeTasks", "active_tasks"),
        "updated_at": _first_present(status, "updatedAt", "updated_at"),
    }


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data.get(key)
    return None


async def _call_runtime_method(runtime: dict[str, Any], name: str, *args: Any) -> Any:
    return await maybe_await(runtime[name](*args))


def _run_background(coro: Any) -> None:
    import asyncio
    import threading

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(target=lambda: asyncio.run(coro), daemon=True).start()
        return
    loop.create_task(coro)
