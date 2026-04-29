from __future__ import annotations

from .shared import *


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
        on_event("mcp.tools_changed", {"server": config.get("name"), "method": method, "params": normalized_params})
    elif method in {"notifications/resources/list_changed", "resources/list_changed"}:
        if state is not None:
            state["resourcesStale"] = True
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


async def _register_requested_channels(states: list[dict[str, Any]], channels: list[str], session_id: str | None, on_event: Callable[..., Any]) -> None:
    if not session_id:
        return
    for server in channels:
        state = next((entry for entry in states if entry["config"].get("name") == server), None)
        if not state:
            on_event("mcp.channel_registration_failed", {"server": server, "session_id": session_id, "reason": "MCP server is not connected."})
            continue
        state["channelRegistration"]["requested"] = True
        if not state.get("connected"):
            state["channelRegistration"].update({"status": "failed", "updatedAt": now_iso(), "reason": state.get("error") or "MCP server is not connected."})
            on_event("mcp.channel_registration_failed", {"server": server, "session_id": session_id, "reason": state["channelRegistration"].get("reason")})
            continue
        try:
            result = await _safe_request(state, "channels/register", {"sessionId": session_id, "session_id": session_id, "client": {"name": "tess", "version": "0.1.0"}})
            state["channelRegistration"].update({"status": "registered", "updatedAt": now_iso()})
            on_event("mcp.channel_registered", {"server": server, "session_id": session_id, "result": result if isinstance(result, dict) else {}})
        except Exception as exc:
            state["channelRegistration"].update({"status": "failed", "updatedAt": now_iso(), "reason": str(exc)})
            on_event("mcp.channel_registration_failed", {"server": server, "session_id": session_id, "reason": str(exc)})


def _select_states(states: list[dict[str, Any]], server: str | None) -> list[dict[str, Any]]:
    return [state for state in states if not server or state["config"].get("name") == server]
