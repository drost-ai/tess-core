from __future__ import annotations

import json
import os
from typing import Any

from ..storage import list_bridges, load_bridge, load_bridge_events, load_bridge_inbox, load_remote_session
from .sessions import _truncate_remote_summary


async def format_bridge_tail(bridgeId: str, limit: int = 20) -> str:
    """The last ``limit`` events of a remote bridge as text, one per line."""
    events = await load_bridge_events(bridgeId)
    if not events:
        return "No bridge events recorded."
    lines = []
    for item in events[-limit:]:
        data = item.get("data") or {}
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            summary = _truncate_remote_summary(data["text"])
        else:
            summary = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        lines.append(f"{item.get('createdAt')}  {item.get('event')}  {summary}")
    return "\n".join(lines)


async def get_bridge_diagnostics(bridgeId: str, limit: int = 20) -> dict[str, Any]:
    bridge = await load_bridge(bridgeId)
    events = await load_bridge_events(bridgeId)
    inbox = await load_bridge_inbox(bridgeId)
    remote = await load_remote_session(bridge.get("remoteSessionId")) if bridge and bridge.get("remoteSessionId") else None
    return {
        "bridge": bridge,
        "remote": remote,
        "status": (bridge or {}).get("status"),
        "remoteStatus": (remote or {}).get("status"),
        "bridgeStatus": (remote or {}).get("bridgeStatus"),
        "pendingMessages": (bridge or {}).get("pendingMessages") if bridge else 0,
        "messageCount": (bridge or {}).get("messageCount") if bridge else 0,
        "inboxCount": len(inbox),
        "events": events[-limit:],
    }


async def list_active_bridge_rows() -> list[str]:
    """One formatted row per active remote bridge, for display."""
    rows = []
    for bridge in await list_bridges():
        if bridge.get("status") != "active":
            continue
        name = bridge.get("name") if bridge.get("name") is not None else os.path.basename(bridge.get("cwd") or "")
        remote = bridge.get("remoteSessionId")[:8] if bridge.get("remoteSessionId") else "-"
        rows.append("  ".join([bridge.get("id", "")[:8], str(name).ljust(18), str(bridge.get("status", "")).ljust(8), remote, str(bridge.get("updatedAt", ""))[:19].replace("T", " ")]))
    return rows


__all__ = ["format_bridge_tail", "get_bridge_diagnostics", "list_active_bridge_rows"]
