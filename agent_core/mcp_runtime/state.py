from __future__ import annotations

from .shared import *


def _dedupe_servers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.get("name"):
            out[server["name"]] = server
    return list(out.values())


def _create_state(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "config": config,
        "connected": False,
        "state": "disconnected",
        "error": None,
        "tools": [],
        "resources": [],
        "client": None,
        "lastConnectedAt": None,
        "lastErrorAt": None,
        "serverCapabilities": {},
        "serverInfo": {},
        "lastNotificationAt": None,
        "channelRegistration": {"requested": False, "status": "not_requested"},
    }


async def _connect_state(state: dict[str, Any], cwd: str, on_event: Callable[..., Any]) -> None:
    state.update({"state": "connecting", "error": None})
    if state.get("client"):
        try:
            await state["client"].close()
        except Exception:
            pass
    try:
        client = _create_client(state["config"], cwd, on_event, state)
        await client.initialize()
        state.update({"client": client, "connected": True, "state": "connected", "lastConnectedAt": now_iso(), "serverCapabilities": client.server_capabilities, "serverInfo": client.server_info})
        state["tools"] = await _refresh_tools(state)
        state["resources"] = await _refresh_resources(state)
        on_event("mcp.server_connected", {"server": state["config"]["name"], "transport": state["config"].get("transport"), "tools": len(state["tools"]), "resources": len(state["resources"])})
    except Exception as exc:
        _mark_state_failure(state, exc)
        on_event("mcp.server_connection_failed", {"server": state["config"].get("name"), "transport": state["config"].get("transport"), "reason": state.get("error")})


def _create_client(config: dict[str, Any], cwd: str, on_event: Callable[..., Any], state: dict[str, Any] | None = None) -> JsonRpcClient:
    def notification(payload: dict[str, Any]) -> None:
        _emit_mcp_notification(config, payload, on_event, state)

    if config.get("transport") == "stdio":
        return StdioMcpClient(config, cwd, notification)
    if config.get("transport") == "sse":
        return SseMcpClient(config, cwd, notification)
    return HttpMcpClient(config, cwd, notification)


async def _ensure_connected(state: dict[str, Any], cwd: str, on_event: Callable[..., Any]) -> dict[str, Any]:
    if state.get("connected") and state.get("client"):
        return state
    await _connect_state(state, cwd, on_event)
    if state.get("connected") and state.get("client"):
        return state
    detail = f" ({state.get('error')})" if state.get("error") else ""
    raise RuntimeError(f"MCP server is not connected: {state['config'].get('name')}{detail}")


async def _require_connected(states: list[dict[str, Any]], server: str, cwd: str, on_event: Callable[..., Any]) -> dict[str, Any]:
    state = next((entry for entry in states if entry["config"].get("name") == server), None)
    if not state:
        raise RuntimeError(f"MCP server not found: {server}")
    return await _ensure_connected(state, cwd, on_event)


async def _request_with_state_tracking(state: dict[str, Any], method: str, params: Any = None) -> Any:
    try:
        return await state["client"].request(method, params)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _mark_state_failure(state, exc)
        raise


async def _refresh_tools(state: dict[str, Any]) -> list[dict[str, Any]]:
    result = await _safe_request(state, "tools/list")
    if result is None:
        return []
    tools = result.get("tools") if isinstance(result, dict) else []
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        out.append(
            {
                "server": state["config"]["name"],
                "name": tool["name"],
                "description": tool.get("description"),
                "inputSchema": tool.get("inputSchema") or tool.get("input_schema") or {"type": "object"},
                "runtimeName": _runtime_tool_name(state["config"]["name"], tool["name"]),
            }
        )
    return out


async def _refresh_resources(state: dict[str, Any]) -> list[dict[str, Any]]:
    result = await _safe_request(state, "resources/list")
    if result is None:
        return state.get("resources") or []
    resources = result.get("resources") if isinstance(result, dict) else []
    out = []
    for item in resources or []:
        if not isinstance(item, dict) or not isinstance(item.get("uri"), str) or not isinstance(item.get("name"), str):
            continue
        out.append(
            {
                "server": state["config"]["name"],
                "uri": item.get("uri"),
                "name": item.get("name"),
                "mimeType": item.get("mimeType"),
                "description": item.get("description"),
            }
        )
    return out


async def _safe_request(state: dict[str, Any], method: str, params: Any = None) -> Any:
    try:
        return await state["client"].request(method, params)
    except Exception as exc:
        if re.search(r"-32601|Method not found|not implemented", str(exc), re.I):
            return None
        _mark_state_failure(state, exc)
        raise


def _server_status(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": state["config"].get("name"),
        "transport": state["config"].get("transport"),
        "connected": state["connected"],
        "state": state["state"],
        "error": state.get("error"),
        "tools": len(state["tools"]),
        "resources": len(state["resources"]),
        "capabilities": {"tools": len(state["tools"]), "resources": len(state["resources"]), "channelsSupported": bool(state["channelRegistration"].get("requested")) or state["channelRegistration"].get("status") == "registered"},
        "serverCapabilities": state.get("serverCapabilities") or {},
        "serverInfo": state.get("serverInfo") or {},
        "lastNotificationAt": state.get("lastNotificationAt"),
        "lastConnectedAt": state.get("lastConnectedAt"),
        "lastErrorAt": state.get("lastErrorAt"),
        "channelRegistration": state["channelRegistration"],
    }


def _mark_state_failure(state: dict[str, Any], error: Exception) -> None:
    state["connected"] = False
    state["state"] = "degraded" if state["tools"] or state["resources"] else "disconnected"
    state["error"] = str(error)
    state["lastErrorAt"] = now_iso()
