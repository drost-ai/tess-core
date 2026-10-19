from __future__ import annotations

import asyncio
import contextlib
import re
import time
import weakref
from typing import Any, Callable

from .._utils import now_iso, pretty_json
from .auth import McpAuthorizationRequiredError, create_mcp_oauth_runtime
from .clients import JsonRpcClient
from .http_transport import HttpMcpClient
from .protocol import McpProtocolError, _runtime_tool_name, _without_none_values
from .sse_transport import SseMcpClient
from .stdio_transport import StdioMcpClient
from .transport_policy import McpTransportError, resolve_mcp_transport_policy


def _dedupe_servers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.get("name"):
            out[server["name"]] = server
    return list(out.values())


def _create_state(
    config: dict[str, Any],
    token_store: Any = None,
    on_event: Callable[..., Any] | None = None,
    authorization_callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    state = {
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
        "toolsStale": False,
        "resourcesStale": False,
        "notificationGeneration": 0,
        "catalogGeneration": 0,
        "connectionGeneration": 0,
        "reconnectFailures": 0,
        "reconnectBackoffUntil": 0.0,
    }
    if config.get("transport") != "stdio" and token_store is not None:
        state["oauth"] = create_mcp_oauth_runtime(
            config=config,
            store=token_store,
            on_runtime_event=on_event,
            authorization_callback=authorization_callback,
        )
    return state


async def _connect_state(state: dict[str, Any], cwd: str, on_event: Callable[..., Any]) -> None:
    state.update({"state": "connecting", "error": None})
    if state.get("client"):
        try:
            await state["client"].close()
        except Exception:
            pass
    try:
        client = _create_client(state["config"], cwd, on_event, state)
        state["client"] = client

        async def startup() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            await client.initialize()
            return await _refresh_tools(state), await _refresh_resources(state)

        startup_timeout_ms = resolve_mcp_transport_policy(state["config"])[
            "startupTimeoutMs"
        ]
        try:
            candidate_tools, candidate_resources = await asyncio.wait_for(
                startup(),
                timeout=startup_timeout_ms / 1000,
            )
        except asyncio.TimeoutError as error:
            raise McpTransportError(
                "startup_timeout",
                f"MCP startup exceeded {startup_timeout_ms}ms for {state['config'].get('name')}.",
            ) from error
        # Discover into candidates so a partial refresh cannot replace the
        # previous known-good catalog on reconnect failure.
        state.update(
            {
                "client": client,
                "connected": True,
                "state": "connected",
                "reconnectFailures": 0,
                "reconnectBackoffUntil": 0.0,
                "lastConnectedAt": now_iso(),
                "serverCapabilities": client.server_capabilities,
                "serverInfo": client.server_info,
                "protocolVersion": client.negotiated_protocol_version,
                "tools": candidate_tools,
                "resources": candidate_resources,
                "toolsStale": False,
                "resourcesStale": False,
                "catalogGeneration": int(state.get("catalogGeneration") or 0) + 1,
                "connectionGeneration": int(state.get("connectionGeneration") or 0) + 1,
                "authorizationUrl": None,
                "authorizationContinuationId": None,
            }
        )
        on_event("mcp.server_connected", {"server": state["config"]["name"], "transport": state["config"].get("transport"), "tools": len(state["tools"]), "resources": len(state["resources"]), "protocol_version": state.get("protocolVersion")})
    except asyncio.CancelledError:
        if 'client' in locals():
            with contextlib.suppress(Exception):
                await client.close()
            if state.get("client") is client:
                state["client"] = None
        raise
    except Exception as exc:
        if 'client' in locals():
            try:
                await client.close()
            except Exception:
                pass
            if state.get("client") is client:
                state["client"] = None
        _mark_state_failure(state, exc)
        if not isinstance(exc, McpAuthorizationRequiredError):
            # Authorization-pending is not a server fault; its retries are
            # already gated on a usable token rather than a timer.
            _arm_reconnect_backoff(state)
        on_event("mcp.server_connection_failed", {"server": state["config"].get("name"), "transport": state["config"].get("transport"), "reason": "authorization_required" if isinstance(exc, McpAuthorizationRequiredError) else state.get("error")})


def _create_client(config: dict[str, Any], cwd: str, on_event: Callable[..., Any], state: dict[str, Any] | None = None) -> JsonRpcClient:
    def notification(payload: dict[str, Any]) -> None:
        _emit_mcp_notification(config, payload, on_event, state)

    if config.get("transport") == "stdio":
        return StdioMcpClient(config, cwd, notification)
    if config.get("transport") == "sse":
        return SseMcpClient(config, cwd, notification, state.get("oauth") if state else None)
    return HttpMcpClient(config, cwd, notification, state.get("oauth") if state else None)


MCP_RECONNECT_BACKOFF_BASE_MS = 1000
MCP_RECONNECT_BACKOFF_MAX_MS = 60_000

# Connect locks are scoped per running event loop (tests and embedders run
# many short-lived loops; an asyncio.Lock cannot cross loops) and keyed by
# state identity, which lives as long as the runtime.
_CONNECT_LOCKS: "weakref.WeakKeyDictionary[Any, dict[int, asyncio.Lock]]" = weakref.WeakKeyDictionary()


def _state_connect_lock(state: dict[str, Any]) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _CONNECT_LOCKS.get(loop)
    if locks is None:
        locks = {}
        _CONNECT_LOCKS[loop] = locks
    lock = locks.get(id(state))
    if lock is None:
        lock = asyncio.Lock()
        locks[id(state)] = lock
    return lock


def _arm_reconnect_backoff(state: dict[str, Any]) -> None:
    failures = int(state.get("reconnectFailures") or 0) + 1
    delay_ms = min(
        MCP_RECONNECT_BACKOFF_MAX_MS,
        MCP_RECONNECT_BACKOFF_BASE_MS * (2 ** (failures - 1)),
    )
    state["reconnectFailures"] = failures
    state["reconnectBackoffUntil"] = time.monotonic() + delay_ms / 1000


async def _ensure_connected(state: dict[str, Any], cwd: str, on_event: Callable[..., Any]) -> dict[str, Any]:
    _reconcile_stdio_transport(state)
    if state.get("connected") and state.get("client"):
        return state
    async with _state_connect_lock(state):
        # A concurrent caller may have finished the reconnect while this one
        # waited on the lock; without it, racing callers spawn duplicate
        # stdio processes and leak the loser.
        if state.get("connected") and state.get("client"):
            return state
        if state.get("state") == "authorization_required" and state.get("oauth"):
            if not await state["oauth"].has_usable_token():
                raise RuntimeError(f"MCP server requires authorization: {state['config'].get('name')}")
        remaining = float(state.get("reconnectBackoffUntil") or 0) - time.monotonic()
        if remaining > 0:
            detail = f" ({state.get('error')})" if state.get("error") else ""
            raise RuntimeError(
                f"MCP server is not connected (reconnect backoff, retry in {max(1, round(remaining))}s): "
                f"{state['config'].get('name')}{detail}"
            )
        await _connect_state(state, cwd, on_event)
        if state.get("connected") and state.get("client"):
            return state
        if state.get("state") == "authorization_required":
            raise RuntimeError(f"MCP server requires authorization: {state['config'].get('name')}")
        detail = f" ({state.get('error')})" if state.get("error") else ""
        raise RuntimeError(f"MCP server is not connected: {state['config'].get('name')}{detail}")


async def _require_connected(states: list[dict[str, Any]], server: str, cwd: str, on_event: Callable[..., Any]) -> dict[str, Any]:
    state = next((entry for entry in states if entry["config"].get("name") == server), None)
    if not state:
        raise RuntimeError(f"MCP server not found: {server}")
    return await _ensure_connected(state, cwd, on_event)


def _is_connection_fatal(error: BaseException) -> bool:
    # A JSON-RPC error response or one slow request comes from a live server;
    # only transport-level faults justify tearing the connection down (which
    # for stdio means respawning the process and losing server-side state).
    if isinstance(error, McpProtocolError):
        return False
    if isinstance(error, McpTransportError) and error.code == "request_timeout":
        return False
    return True


async def _request_with_state_tracking(state: dict[str, Any], method: str, params: Any = None) -> Any:
    try:
        return await state["client"].request(method, params)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if _is_connection_fatal(exc):
            _mark_state_failure(state, exc)
        raise


MCP_LIST_PAGE_MAX = 64


async def _paginated_list(state: dict[str, Any], method: str, key: str) -> list[Any] | None:
    """Collect every page of a cursor-paginated list; None if the method is unsupported."""
    items: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _page in range(MCP_LIST_PAGE_MAX):
        params = {"cursor": cursor} if cursor is not None else None
        result = await _safe_request(state, method, params)
        if result is None:
            return items if items else None
        page = result.get(key) if isinstance(result, dict) else None
        if isinstance(page, list):
            items.extend(page)
        next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            return items
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return items


async def _refresh_tools(state: dict[str, Any]) -> list[dict[str, Any]]:
    tools = await _paginated_list(state, "tools/list", "tools")
    if tools is None:
        return []
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
                **({"annotations": annotations} if (annotations := _normalize_tool_annotations(tool.get("annotations"))) is not None else {}),
            }
        )
    return out


def _normalize_tool_annotations(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    annotations: dict[str, Any] = {}
    if isinstance(value.get("title"), str):
        annotations["title"] = value["title"]
    for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        if isinstance(value.get(key), bool):
            annotations[key] = value[key]
    return annotations or None


async def _refresh_resources(state: dict[str, Any]) -> list[dict[str, Any]]:
    resources = await _paginated_list(state, "resources/list", "resources")
    if resources is None:
        return state.get("resources") or []
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
        if _is_connection_fatal(exc):
            _mark_state_failure(state, exc)
        raise


def _reconcile_stdio_transport(state: dict[str, Any]) -> None:
    client = state.get("client")
    if state.get("connected") and isinstance(client, StdioMcpClient):
        with client.state_lock:
            closed = client.state != "open"
        if closed:
            # Transport threads own process cleanup, not this catalog cache.
            # Reconcile on observation without starting a retry/watchdog loop.
            _mark_state_failure(state, McpTransportError(
                "transport_closed", "MCP stdio transport is no longer running."
            ))
            _arm_reconnect_backoff(state)


def _server_status(state: dict[str, Any]) -> dict[str, Any]:
    _reconcile_stdio_transport(state)
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
        "protocolVersion": state.get("protocolVersion"),
        "catalogGeneration": state.get("catalogGeneration", 0),
        "connectionGeneration": state.get("connectionGeneration", 0),
        "toolsStale": bool(state.get("toolsStale")),
        "authorizationUrl": state.get("authorizationUrl"),
        "authorizationContinuationId": state.get("authorizationContinuationId"),
    }


def _mark_state_failure(state: dict[str, Any], error: Exception) -> None:
    if isinstance(error, McpAuthorizationRequiredError):
        state["connected"] = False
        state["state"] = "authorization_required"
        state["error"] = str(error)
        state["authorizationUrl"] = error.authorization_url
        state["authorizationContinuationId"] = error.continuation_id
        state["lastErrorAt"] = now_iso()
        return
    state["connected"] = False
    if isinstance(error, McpTransportError) and error.code == "startup_timeout":
        state["state"] = "unavailable"
        state["error"] = str(error)
        state["lastErrorAt"] = now_iso()
        return
    state["state"] = "degraded" if state["tools"] or state["resources"] else "disconnected"
    state["error"] = str(error)
    state["lastErrorAt"] = now_iso()


def _emit_mcp_notification(config: dict[str, Any], payload: dict[str, Any], on_event: Callable[..., Any], state: dict[str, Any] | None = None) -> None:
    method = payload.get("method")
    params = payload.get("params")
    normalized_params = params if isinstance(params, dict) else {"payload": params}
    if state is not None:
        state["lastNotificationAt"] = now_iso()
    on_event(
        "mcp.channel_notification",
        {
            "server": config.get("name"),
            "method": method,
            "params": normalized_params,
        },
    )
    if method in {"notifications/tools/list_changed", "tools/list_changed"}:
        if state is not None:
            state["toolsStale"] = True
            state["notificationGeneration"] = int(state.get("notificationGeneration") or 0) + 1
        on_event("mcp.tools_changed", {"server": config.get("name"), "method": method, "params": normalized_params})
    elif method in {"notifications/resources/list_changed", "resources/list_changed"}:
        if state is not None:
            state["resourcesStale"] = True
            state["notificationGeneration"] = int(state.get("notificationGeneration") or 0) + 1
        on_event("mcp.resources_changed", {"server": config.get("name"), "method": method, "params": normalized_params})
    elif method in {"notifications/message", "notifications/logging/message", "logging/message"}:
        level = normalized_params.get("level") or normalized_params.get("severity")
        message = normalized_params.get("message") or normalized_params.get("text") or normalized_params.get("data") or normalized_params.get("payload")
        on_event(
            "mcp.log",
            _without_none_values(
                {
                    "server": config.get("name"),
                    "method": method,
                    "level": level if isinstance(level, str) else None,
                    "message": message if isinstance(message, str) else pretty_json(message) if message is not None else None,
                    "params": normalized_params,
                }
            ),
        )
