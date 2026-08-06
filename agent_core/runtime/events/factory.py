from __future__ import annotations

from ...contracts.events import RuntimeEventEnvelope

from .shared import *

def createRuntimeEvent(args: dict[str, Any] | None = None, **kwargs: Any) -> RuntimeEventEnvelope:
    args = {**(args or {}), **kwargs}
    legacy_event = args.get("event") or args.get("type") or args.get("kind")
    raw_data = args["data"] if "data" in args and args.get("data") is not None else args.get("payload")
    data = raw_data if isinstance(raw_data, dict) else {}
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
