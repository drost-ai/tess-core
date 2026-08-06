from __future__ import annotations

from typing import Any, Callable

from .._utils import now_iso
from .state import _safe_request



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
