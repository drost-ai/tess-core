from __future__ import annotations

from .shared import *


async def createMcpRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd = args.get("cwd") or os.getcwd()
    on_event = args.get("onRuntimeEvent") or (lambda *_: None)
    states = [_create_state(config) for config in _dedupe_servers(args.get("servers") or args.get("mcpConfig") or [])]
    await asyncio.gather(*(_connect_state(state, cwd, on_event) for state in states))
    await _register_requested_channels(states, args.get("registeredChannels") or [], args.get("sessionId"), on_event)

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
        selected = _select_states(states, server)
        out: list[dict[str, Any]] = []
        for state in selected:
            await _ensure_connected(state, cwd, on_event)
            state["resources"] = await _refresh_resources(state)
            out.extend(state["resources"])
        return out

    async def read_resource(server: str, uri: str) -> dict[str, Any]:
        state = await _require_connected(states, server, cwd, on_event)
        result = await _request_with_state_tracking(state, "resources/read", {"uri": uri})
        return await _normalize_read_resource_output(server, result)

    async def call_tool(server: str, name: str, tool_args: dict[str, Any] | None = None) -> dict[str, Any]:
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
                        result = await call_tool(_tool["server"], _tool["name"], input)
                        _throw_if_runtime_tool_cancelled(_context)
                        return result
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
                    )
                )
        return definitions

    async def close() -> None:
        await asyncio.gather(*(state["client"].close() for state in states if state.get("client")), return_exceptions=True)

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
        "close": close,
    }
