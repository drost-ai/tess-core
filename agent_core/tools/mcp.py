from __future__ import annotations

from ..contracts.context import ToolContext

from typing import Any

from .._utils import pretty_json
from ..tool_payloads import shapeTextToolResult
from .cancellation import _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .schemas import _tool_description, _tool_schema
from .validation import _require_string
from .web import _facade_fetch, _header_value


def _mcp_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        _throw_if_tool_cancelled(context)
        if not runtime:
            return {
                "content": pretty_json(
                    {
                        "servers": [
                            {
                                "name": server.get("name"),
                                "transport": server.get("transport"),
                                "connected": False,
                                "state": "disconnected",
                                "tools": 0,
                                "resources": 0,
                                "capabilities": {"tools": 0, "resources": 0, "channelsSupported": False},
                                "channelRegistration": {"requested": False, "status": "not_requested"},
                            }
                            for server in context["config"].get("mcpConfig", [])
                        ],
                        "tools": [],
                    }
                )
            }
        diagnostics = runtime["getDiagnostics"]() if callable(runtime.get("getDiagnostics")) else runtime.get("diagnostics", {})
        _throw_if_tool_cancelled(context)
        tools = [
            {"server": tool.get("server"), "name": tool.get("name"), "runtime_name": tool.get("runtimeName") or tool.get("runtime_name"), "description": tool.get("description")}
            for tool in runtime["listTools"]()
        ]
        _throw_if_tool_cancelled(context)
        return {"content": pretty_json({"diagnostics": diagnostics, "servers": runtime["listServerStatus"](), "tools": tools})}

    return ToolDefinition("Mcp", _tool_description("Mcp"), _tool_schema("Mcp"), handler)


def _list_mcp_resources_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        server = input.get("server") if isinstance(input.get("server"), str) else None
        if runtime:
            _throw_if_tool_cancelled(context)
            resources = await runtime["listResources"](server)
            _throw_if_tool_cancelled(context)
            return {"content": pretty_json(resources)}
        configs = context["config"].get("mcpConfig", [])
        return {"content": pretty_json([{"server": item.get("name"), "name": item.get("name"), "uri": item.get("commandOrUrl"), "description": f"{item.get('transport')} server (not connected)"} for item in configs if not server or item.get("name") == server])}

    return ToolDefinition("ListMcpResources", _tool_description("ListMcpResources"), _tool_schema("ListMcpResources"), handler)


def _read_mcp_resource_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        server = _require_string(input.get("server"), "server")
        uri = _require_string(input.get("uri"), "uri")
        if runtime:
            _throw_if_tool_cancelled(context)
            resource = await runtime["readResource"](server, uri)
            _throw_if_tool_cancelled(context)
            return {"content": pretty_json(resource)}
        config = next((entry for entry in context["config"].get("mcpConfig", []) if entry.get("name") == server), None)
        if not config:
            return {"content": f"MCP server not found: {server}", "isError": True}
        if config.get("transport") in ("http", "sse"):
            _throw_if_tool_cancelled(context)
            response = await _facade_fetch()(uri, headers=config.get("headers"), signal=context.get("abortSignal"))
            _throw_if_tool_cancelled(context)
            text = await response.text()
            shaped = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="ReadMcpResource", family="web", text=text, strategy="head")
            return {"content": pretty_json({"contents": [{"uri": uri, "mimeType": _header_value(response.headers, "content-type"), "text": shaped["text"], "manifest": shaped["view"]}]})}
        return {"content": f"MCP server not found: {server}", "isError": True}

    return ToolDefinition("ReadMcpResource", _tool_description("ReadMcpResource"), _tool_schema("ReadMcpResource"), handler)
