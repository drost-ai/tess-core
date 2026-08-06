from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

from .._utils import maybe_await
from .exception_groups import RuntimeExceptionGroup
from ..authority.executable_source_policy import selectMcpBootstrapServers


async def prepareRuntimeSessionComponents(args: Mapping[str, Any]) -> dict[str, Any]:
    session = args["session"]
    config = args["config"]
    binding = args["binding"]
    factories = args["factories"]
    bootstrap_args = args.get("bootstrapArgs") or {}
    emit = args["emit"]
    owned_closers: list[Callable[[], Any]] = []
    closed = False
    deferred_workspace_activation = False
    has_session = bool(args.get("hasSession", True))

    try:
        authority = (
            await factories["enforceAuthority"](session=session, config=config)
            if has_session
            else None
        )
        workspace = bootstrap_args.get("workspaceController")
        if workspace is None and has_session:
            workspace = factories["createWorkspace"]({"session": session, "config": config})
            refresh = getattr(workspace, "refresh", None)
            if callable(refresh):
                await maybe_await(refresh())
        else:
            deferred_workspace_activation = True

        plugins = await factories["loadPlugins"](
            config,
            {
                "pinnedInstalledRecords": copy.deepcopy(
                    authority["executableSources"].get("pluginRecords") or []
                )
            }
            if authority
            else None,
        )
        plugin_mcp_servers = plugins.get("mcpServers") or []
        mcp_runtime = bootstrap_args.get("mcpRuntime")
        mcp_servers = bootstrap_args.get("mcpConfig") or config.get("mcpConfig") or []
        selected_mcp_servers = selectMcpBootstrapServers(
            config.get("mcpConfigPolicy"), mcp_servers, plugin_mcp_servers
        )
        if mcp_runtime is None and selected_mcp_servers:
            mcp_runtime = await factories["createMcp"](
                {
                    "cwd": config.get("cwd") or session.get("cwd"),
                    "servers": selected_mcp_servers,
                    "sessionId": session.get("id"),
                    "registeredChannels": bootstrap_args.get("registeredChannels")
                    or config.get("channels")
                    or [],
                    "onRuntimeEvent": emit,
                    "mcpTokenStore": bootstrap_args.get("mcpTokenStore"),
                    "mcpAuthorizationCallback": bootstrap_args.get(
                        "mcpAuthorizationCallback"
                    ),
                }
            )
            if callable(mcp_runtime.get("close")):
                owned_closers.append(mcp_runtime["close"])

        bridge_runtime = bootstrap_args.get("bridgeRuntime") or bootstrap_args.get("bridge")
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        if bridge_runtime is None and has_session and (
            bootstrap_args.get("enableBridge") or metadata.get("remoteControlEnabled")
        ):
            bridge_runtime = await factories["createBridge"](
                {
                    "session": session,
                    "bridgeId": metadata.get("bridgeId"),
                    "remoteSessionId": metadata.get("remoteSessionId"),
                    "name": bootstrap_args.get("bridgeName"),
                    "emit": emit,
                    "runtimePaths": bootstrap_args.get("runtimePaths"),
                    **(
                        {"startPolling": bootstrap_args["bridgeStartPolling"]}
                        if "bridgeStartPolling" in bootstrap_args
                        else {}
                    ),
                }
            )
            if callable(bridge_runtime.get("close")):
                owned_closers.insert(0, bridge_runtime["close"])

        tool_options = {
            **bootstrap_args,
            "mcpRuntime": mcp_runtime,
            "workspaceController": workspace,
        }
        plugin_tools = plugins.get("tools") or []

        def compose_tools() -> list[Any]:
            raw = [
                *factories["createBuiltinTools"](tool_options),
                *(mcp_runtime["createToolDefinitions"]() if mcp_runtime else []),
                *plugin_tools,
            ]
            return factories["filterAuthorityTools"](_dedupe_tools(raw), authority)

        tools = compose_tools()
        slash_commands = _dedupe_slash_commands(plugins.get("commands") or [])
        if authority is None:
            factories["augmentConfigTools"](
                config, [_tool_name(tool) for tool in tools if _tool_name(tool)]
            )

        async def refresh_mcp_tools() -> dict[str, Any]:
            nonlocal tools
            if not mcp_runtime or not callable(mcp_runtime.get("applyPendingCatalogChanges")):
                return {"changed": False, "servers": [], "generation": 0, "tools": None}
            result = await maybe_await(mcp_runtime["applyPendingCatalogChanges"]())
            if result.get("changed"):
                tools = compose_tools()
                components["tools"] = tools
                if authority is None:
                    factories["augmentConfigTools"](
                        config, [_tool_name(tool) for tool in tools if _tool_name(tool)]
                    )
            return {**result, "tools": tools if result.get("changed") else None}

        async def activate_deferred_workspace() -> None:
            if not deferred_workspace_activation:
                return
            setter = getattr(workspace, "setSession", None)
            if callable(setter):
                await maybe_await(setter(session))
            refresh = getattr(workspace, "refresh", None)
            if callable(refresh):
                await maybe_await(refresh())

        async def close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            failures: list[BaseException] = []
            for closer in owned_closers:
                try:
                    await maybe_await(closer())
                except BaseException as exc:
                    failures.append(exc)
            if failures:
                raise RuntimeExceptionGroup(
                    "One or more session runtime components failed to close.", failures
                )

        components = {
            "binding": binding,
            "session": session,
            "config": config,
            "workspace": workspace,
            "plugins": plugins,
            "mcp": mcp_runtime,
            "bridge": bridge_runtime,
            "tools": tools,
            "slashCommands": slash_commands,
            "refreshMcpTools": refresh_mcp_tools,
            "activateDeferredWorkspace": activate_deferred_workspace,
            "deferredWorkspaceActivation": deferred_workspace_activation,
            "close": close,
        }
        return components
    except BaseException:
        for closer in owned_closers:
            try:
                await maybe_await(closer())
            except BaseException:
                pass
        raise


def _dedupe_tools(tools: list[Any]) -> list[Any]:
    seen: set[str] = set()
    output: list[Any] = []
    for tool in tools:
        name = _tool_name(tool)
        if not name or name in seen:
            continue
        seen.add(name)
        output.append(tool)
    return output


def _dedupe_slash_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for command in commands:
        name = str(command.get("name") or "").lower()
        if not name or name in seen:
            continue
        seen.add(name)
        output.append(command)
    return output


def _tool_name(tool: Any) -> str | None:
    return getattr(tool, "name", None) or (
        tool.get("name") if isinstance(tool, dict) else None
    )


prepare_runtime_session_components = prepareRuntimeSessionComponents
