from __future__ import annotations

from typing import Any

from ._utils import now_iso


KNOWN_RUNTIME_EVENTS = {
    "session.activated",
    "message.user",
    "message.assistant",
    "message.assistant.partial",
    "transcript.repaired",
    "turn.started",
    "turn.cancelling",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "status.updated",
    "status.cleared",
    "provider.retrying",
    "provider.recovered",
    "provider.failed",
    "provider.input_tokens",
    "provider.fallback",
    "tool.started",
    "tool.completed",
    "permission.requested",
    "permission.decided",
    "plan.requested",
    "plan.approved",
    "plan.rejected",
    "plan.contract_enforced",
    "compaction.started",
    "compaction.skipped",
    "compaction.completed",
    "compaction.failed",
    "context_relief.selected",
    "context_relief.applied",
    "daemon.snapshot",
    "daemon.recovering",
    "daemon.recovered",
    "task.snapshot",
    "task.registered",
    "task.updated",
    "task.output_delta",
    "task.completed",
    "task.failed",
    "task.restored",
    "health_check.unhealthy",
    "health_check.recovered",
    "monitor.snapshot",
    "monitor.created",
    "monitor.assistant_update",
    "monitor.status_changed",
    "collaboration.snapshot",
    "collaboration.assistant_update",
    "bridge.message",
}


def createRuntimeEvent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    legacy_event = args.get("event") or args.get("type") or args.get("kind")
    data = args.get("data") or args.get("payload") or {}
    timestamp = args.get("timestamp") or args.get("createdAt") or now_iso()
    session_id = _field(data, "session_id", "sessionId") or args.get("defaultSessionId")
    kind, payload = _typed_payload(str(legacy_event or ""), data)
    return {
        "event": legacy_event,
        "type": legacy_event,
        "kind": kind,
        "data": data,
        "payload": payload,
        "createdAt": timestamp,
        "timestamp": timestamp,
        "sessionId": session_id,
        "legacyEvent": legacy_event,
        "legacyData": data,
    }


def _typed_payload(event: str, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    payload = _camelize_record(data)
    if event in KNOWN_RUNTIME_EVENTS:
        return event, payload
    if event.startswith("bridge."):
        return "bridge.event", {"eventName": event, "bridgeId": _field(data, "bridge_id", "bridgeId"), "remoteSessionId": _field(data, "remote_session_id", "remoteSessionId")}
    if event.startswith("remote."):
        return "remote.event", {"eventName": event, "remoteSessionId": _field(data, "remote_session_id", "remoteSessionId")}
    if event.startswith("mcp."):
        return "mcp.event", {"eventName": event, "channelName": _field(data, "channel_name", "channelName")}
    if event.startswith("platform."):
        return "platform.event", {"eventName": event}
    if event.startswith("subagent."):
        return "subagent.event", {"eventName": event}
    if event == "budget.exceeded":
        return "budget.exceeded", payload
    return "runtime.unknown", {"eventName": event}


def _field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data.get(name)
    return None


def _camelize_record(value: Any) -> Any:
    if isinstance(value, list):
        return [_camelize_record(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {_snake_to_camel(str(key)): _camelize_record(item) for key, item in value.items()}


def _snake_to_camel(value: str) -> str:
    if "_" not in value:
        return value
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
