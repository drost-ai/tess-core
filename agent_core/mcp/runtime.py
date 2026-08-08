from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from ..tools import ToolDefinition
from .cancellation import _is_runtime_tool_abort_error, _throw_if_runtime_tool_cancelled
from .normalization import _normalize_read_resource_output, _normalize_tool_call_result
from .notifications import _register_requested_channels, _select_states
from .state import (
    _connect_state,
    _create_state,
    _dedupe_servers,
    _ensure_connected,
    _mark_state_failure,
    _refresh_resources,
    _refresh_tools,
    _request_with_state_tracking,
    _require_connected,
    _server_status,
)
from .token_store import createFileMcpTokenStore
from ..runtime.secrets import resolveMcpServerConfigSecrets


async def _ensure_mcp_tool_permission(context: Any, tool: dict[str, Any], tool_args: dict[str, Any] | None) -> None:
    from ..tools.permissions import _ensure_tool_permission

    if (
        not isinstance(context, dict)
        or not isinstance(context.get("session"), dict)
        or not isinstance(context.get("config"), dict)
    ):
        raise PermissionError(
            f"MCP tool {tool.get('runtimeName')} requires a session-bound tool context to evaluate permissions."
        )
    try:
        preview = json.dumps(tool_args or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        preview = str(tool_args)
    if len(preview) > 400:
        preview = preview[:400] + "…"
    await _ensure_tool_permission(
        context,
        tool["runtimeName"],
        "mcp",
        f"Call MCP tool {tool['name']} on server {tool['server']}\n{preview}",
    )


async def createMcpRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd = args.get("cwd") or os.getcwd()
    on_event = args.get("onRuntimeEvent") or (lambda *_: None)
    token_store = args.get("tokenStore") or args.get("mcpTokenStore") or createFileMcpTokenStore()
    authorization_callback = args.get("authorizationCallback") or args.get("mcpAuthorizationCallback")
    resolved_servers = await asyncio.gather(*(
        resolveMcpServerConfigSecrets(config)
        for config in _dedupe_servers(args.get("servers") or args.get("mcpConfig") or [])
    ))
    states = [
        _create_state(config, token_store, on_event, authorization_callback)
        for config in resolved_servers
    ]
    await asyncio.gather(*(_connect_state(state, cwd, on_event) for state in states))
    await _register_requested_channels(states, args.get("registeredChannels") or [], args.get("sessionId"), on_event)
    catalog_lock = asyncio.Lock()
    tool_registry_generation = 1 if any(state.get("connected") for state in states) else 0
    closed = False

    def assert_open() -> None:
        if closed:
            raise RuntimeError("MCP runtime lifecycle is closed.")

    def list_server_status() -> list[dict[str, Any]]:
        return [_server_status(state) for state in states]

    def diagnostics() -> dict[str, Any]:
        statuses = list_server_status()
        return {
            "servers": statuses,
            "connectedServers": sum(1 for state in states if state["connected"]),
            "totalTools": sum(len(state["tools"]) for state in states),
            "totalResources": sum(len(state["resources"]) for state in states),
        }

    def list_tools() -> list[dict[str, Any]]:
        return [tool for state in states for tool in state["tools"]]

    async def list_resources(server: str | None = None) -> list[dict[str, Any]]:
        assert_open()
        selected = _select_states(states, server)

        async def refresh(state: dict[str, Any]) -> list[dict[str, Any]]:
            await _ensure_connected(state, cwd, on_event)
            state["resources"] = await _refresh_resources(state)
            return state["resources"]

        # Servers list concurrently; in the all-servers sweep a dead server
        # is skipped (its failure lives in server status) instead of failing
        # the whole listing or serializing N startup timeouts.
        results = await asyncio.gather(
            *(refresh(state) for state in selected), return_exceptions=True
        )
        out: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError) or server:
                    raise result
                continue
            out.extend(result)
        return out

    async def read_resource(server: str, uri: str) -> dict[str, Any]:
        assert_open()
        state = await _require_connected(states, server, cwd, on_event)
        result = await _request_with_state_tracking(state, "resources/read", {"uri": uri})
        return await _normalize_read_resource_output(server, result)

    async def call_tool(server: str, name: str, tool_args: dict[str, Any] | None = None) -> dict[str, Any]:
        assert_open()
        state = await _require_connected(states, server, cwd, on_event)
        result = await _request_with_state_tracking(state, "tools/call", {"name": name, "arguments": tool_args or {}})
        return _normalize_tool_call_result(server, name, result)

    def create_tool_definitions() -> list[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for state in states:
            for tool in state["tools"]:
                async def handler(input: dict[str, Any], _context: dict[str, Any], _tool=tool) -> dict[str, Any]:
                    try:
                        _throw_if_runtime_tool_cancelled(_context)
                        await _ensure_mcp_tool_permission(_context, _tool, input)
                        result = await call_tool(_tool["server"], _tool["name"], input)
                        _throw_if_runtime_tool_cancelled(_context)
                        return result
                    except PermissionError:
                        raise
                    except Exception as exc:
                        if _is_runtime_tool_abort_error(exc, _context.get("abortSignal") if isinstance(_context, dict) else None):
                            raise
                        return {"content": str(exc), "isError": True}

                definitions.append(
                    ToolDefinition(
                        tool["runtimeName"],
                        f"MCP {tool['server']}: {tool['description']}" if tool.get("description") else f"MCP {tool['server']} tool {tool['name']}",
                        tool.get("inputSchema") or {"type": "object"},
                        handler,
                        concurrency="parallel" if (tool.get("annotations") or {}).get("readOnlyHint") is True else "exclusive",
                        mcp={
                            "server": tool["server"],
                            "name": tool["name"],
                            **({"annotations": tool["annotations"]} if tool.get("annotations") else {}),
                        },
                    )
                )
        return definitions

    def has_pending_catalog_changes() -> bool:
        return any(state.get("toolsStale") or state.get("resourcesStale") for state in states)

    async def apply_pending_catalog_changes() -> dict[str, Any]:
        nonlocal tool_registry_generation
        assert_open()
        async with catalog_lock:
            changed_servers: list[str] = []
            tools_changed = False
            resources_changed = False
            for state in states:
                refresh_tools = bool(state.get("toolsStale"))
                refresh_resources = bool(state.get("resourcesStale"))
                if not refresh_tools and not refresh_resources:
                    continue
                if not state.get("connected") or not state.get("client"):
                    continue
                state["toolsStale"] = False if refresh_tools else state.get("toolsStale", False)
                state["resourcesStale"] = False if refresh_resources else state.get("resourcesStale", False)
                try:
                    candidate_tools = await _refresh_tools(state) if refresh_tools else state["tools"]
                    candidate_resources = await _refresh_resources(state) if refresh_resources else state["resources"]
                except Exception as exc:
                    if refresh_tools:
                        state["toolsStale"] = True
                    if refresh_resources:
                        state["resourcesStale"] = True
                    _mark_state_failure(state, exc)
                    on_event(
                        "mcp.catalog_refresh_failed",
                        {"server": state["config"].get("name"), "catalog": "tools" if refresh_tools else "resources"},
                    )
                    continue
                if refresh_tools:
                    state["tools"] = candidate_tools
                    tools_changed = True
                if refresh_resources:
                    state["resources"] = candidate_resources
                    resources_changed = True
                state["catalogGeneration"] = int(state.get("catalogGeneration") or 0) + 1
                changed_servers.append(state["config"]["name"])
            if changed_servers:
                tool_registry_generation += 1
                on_event(
                    "mcp.catalog_changed_applied",
                    {
                        "servers": changed_servers[:64],
                        "generation": tool_registry_generation,
                        "tools_changed": tools_changed,
                        "resources_changed": resources_changed,
                    },
                )
            return {
                "changed": bool(changed_servers),
                "servers": changed_servers,
                "generation": tool_registry_generation,
                "toolsChanged": tools_changed,
                "resourcesChanged": resources_changed,
            }

    async def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        await asyncio.gather(
            *(state["client"].close() for state in states if state.get("client")),
            *(state["oauth"].close() for state in states if state.get("oauth")),
            return_exceptions=True,
        )
        for state in states:
            state["connected"] = False
            state["state"] = "closed"

    return {
        "tools": list_tools(),
        "resources": [resource for state in states for resource in state["resources"]],
        "servers": list_server_status(),
        "diagnostics": diagnostics(),
        "listServerStatus": list_server_status,
        "getDiagnostics": diagnostics,
        "listTools": list_tools,
        "listResources": list_resources,
        "readResource": read_resource,
        "callTool": call_tool,
        "createToolDefinitions": create_tool_definitions,
        "hasPendingCatalogChanges": has_pending_catalog_changes,
        "hasPendingToolChanges": has_pending_catalog_changes,
        "applyPendingCatalogChanges": apply_pending_catalog_changes,
        "applyPendingToolChanges": apply_pending_catalog_changes,
        "toolRegistryGeneration": lambda: tool_registry_generation,
        "close": close,
    }
