from __future__ import annotations

import uuid
from typing import Any

from ._utils import now_iso


async def createMonitorRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return {"start": lambda: None, "stop": lambda: None, "snapshot": lambda: {"monitors": []}}


def defaultMonitorCheckIntervalMs(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 5000


def defaultMonitorHeartbeatIntervalMs(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 30000


def createMonitorRecord(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    now = now_iso()
    return {"id": args.get("id") or str(uuid.uuid4()), "sessionId": args["sessionId"], "taskId": args["taskId"], "createdAt": now, "updatedAt": now, "status": "active", "goal": args.get("goal", ""), "checkIntervalMs": defaultMonitorCheckIntervalMs(args.get("checkIntervalMs")), "heartbeatIntervalMs": defaultMonitorHeartbeatIntervalMs(args.get("heartbeatIntervalMs"))}


def formatMonitorSummary(monitors: list[dict[str, Any]]) -> str:
    return "\n".join(f"{m.get('id')} {m.get('status')} {m.get('goal', '')}" for m in monitors)
