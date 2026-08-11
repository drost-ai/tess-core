from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

from typing import Any

from .._utils import pretty_json
from ..authority.mcp_origin_policy import fetch_mcp_resource_with_bound_credentials
from ..mcp.payloads import shape_mcp_resource_list, shape_mcp_resource_read
from ..payloads.tools import shape_text_tool_result
from .cancellation import _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .schemas import builtin_tool
from .validation import _require_string
from .web import _header_value


def _mcp_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
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

    return builtin_tool("Mcp", handler)


def _list_mcp_resources_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        server = input.get("server") if isinstance(input.get("server"), str) else None
        payload_context = {"config": context["config"], "sessionId": context["session"]["id"]}
        if runtime:
            _throw_if_tool_cancelled(context)
            resources = await runtime["listResources"](server)
            _throw_if_tool_cancelled(context)
            payload = await shape_mcp_resource_list(
                resources=resources if isinstance(resources, list) else [],
                server=server,
                context=payload_context,
                **_pagination_args(input),
            )
            return {"content": pretty_json(payload)}
        configs = context["config"].get("mcpConfig", [])
        resources = [{"server": item.get("name"), "name": item.get("name"), "uri": item.get("commandOrUrl"), "description": f"{item.get('transport')} server (not connected)"} for item in configs if not server or item.get("name") == server]
        payload = await shape_mcp_resource_list(
            resources=resources,
            server=server,
            context=payload_context,
            **_pagination_args(input),
        )
        return {"content": pretty_json(payload)}

    return builtin_tool("ListMcpResources", handler)


def _read_mcp_resource_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        server = _require_string(input.get("server"), "server")
        uri = _require_string(input.get("uri"), "uri")
        payload_context = {"config": context["config"], "sessionId": context["session"]["id"]}
        if runtime:
            _throw_if_tool_cancelled(context)
            resource = await runtime["readResource"](server, uri)
            _throw_if_tool_cancelled(context)
            payload = await shape_mcp_resource_read(
                server=server,
                uri=uri,
                value=resource,
                context=payload_context,
                **_pagination_args(input),
            )
            return {"content": pretty_json(payload)}
        config = next((entry for entry in context["config"].get("mcpConfig", []) if entry.get("name") == server), None)
        if not config:
            return {"content": f"MCP server not found: {server}", "isError": True}
        if config.get("transport") in ("http", "sse"):
            _throw_if_tool_cancelled(context)
            response = await fetch_mcp_resource_with_bound_credentials(
                config=config,
                uri=uri,
                signal=context.get("abortSignal"),
            )
            _throw_if_tool_cancelled(context)
            text = await response.text()
            payload = await shape_mcp_resource_read(
                server=server,
                uri=uri,
                value={"contents": [{"uri": uri, "mimeType": _header_value(response.headers, "content-type"), "text": text}]},
                context=payload_context,
                **_pagination_args(input),
            )
            return {"content": pretty_json(payload)}
        return {"content": f"MCP server not found: {server}", "isError": True}

    return builtin_tool("ReadMcpResource", handler)


def _pagination_args(input: dict[str, Any]) -> dict[str, Any]:
    return {
        **({"offset": input["offset"]} if "offset" in input else {}),
        **({"cursor": input["cursor"]} if "cursor" in input else {}),
        **({"requestedLimit": input["limit"]} if "limit" in input else {}),
    }
