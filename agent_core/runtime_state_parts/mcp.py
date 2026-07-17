from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlsplit, urlunsplit


MCP_RUNTIME_STATE_EVENTS = {
    "plan.mcp_tool_allowed",
    "plan.mcp_tool_blocked",
    "mcp.authorization_required",
    "mcp.server_connected",
    "mcp.server_connection_failed",
    "mcp.catalog_changed_applied",
    "mcp.catalog_refresh_failed",
}


def reduce_mcp_runtime_state(
    state: dict[str, Any],
    event_type: str,
    data: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    mcp = copy.deepcopy(state.get("mcp") or {})
    if event_type in {"plan.mcp_tool_allowed", "plan.mcp_tool_blocked"}:
        mcp["lastPlanDecision"] = {
            "decision": "allow" if event_type.endswith("allowed") else "block",
            "server": _field(data, "server"),
            "tool": _field(data, "tool"),
            "reason": _field(data, "reason"),
            "updatedAt": timestamp,
        }
        state["mcp"] = mcp
        return state

    server = _field(data, "server") or "unknown"
    servers = copy.deepcopy(mcp.get("servers") or {})
    if event_type == "mcp.authorization_required":
        servers[server] = {
            **servers.get(server, {}),
            "state": "authorization_required",
            "transport": _field(data, "transport"),
            "authorizationUrl": _durable_authorization_url(
                _field(data, "authorizationUrl", "authorization_url")
            ),
            "continuationId": _field(data, "continuationId", "continuation_id"),
            "updatedAt": timestamp,
        }
    elif event_type == "mcp.server_connected":
        servers[server] = {
            "state": "connected",
            "transport": _field(data, "transport"),
            "tools": _field(data, "tools"),
            "resources": _field(data, "resources"),
            "protocolVersion": _field(data, "protocolVersion", "protocol_version"),
            "authorizationUrl": None,
            "continuationId": None,
            "updatedAt": timestamp,
        }
    elif event_type == "mcp.server_connection_failed":
        reason = _field(data, "reason")
        servers[server] = {
            **servers.get(server, {}),
            "state": "authorization_required" if reason == "authorization_required" else "disconnected",
            "transport": _field(data, "transport"),
            "error": reason,
            "updatedAt": timestamp,
        }
    elif event_type == "mcp.catalog_changed_applied":
        mcp.update(
            {
                "catalogGeneration": _field(data, "generation") or mcp.get("catalogGeneration", 0),
                "lastCatalogServers": _field(data, "servers") or [],
                "updatedAt": timestamp,
            }
        )
    elif event_type == "mcp.catalog_refresh_failed":
        servers[server] = {
            **servers.get(server, {}),
            "state": "degraded",
            "failedCatalog": _field(data, "catalog"),
            "updatedAt": timestamp,
        }
    mcp["servers"] = servers
    state["mcp"] = mcp
    return state


def _field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data.get(name)
    return None


def _durable_authorization_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
