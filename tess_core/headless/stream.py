from __future__ import annotations

import json
from typing import Any, TextIO

from ..payloads.media_persistence import sanitize_media_for_persistence
from ..runtime.events import create_runtime_event


class JsonStreamSink:
    def __init__(self, stream: TextIO | None = None, *, max_string_chars: int = 12000) -> None:
        self.stream = stream
        self.max_string_chars = max_string_chars
        self.permission_denied = False
        self.event_count = 0

    def handle_runtime_envelope(self, envelope: dict[str, Any]) -> None:
        typed = envelope.get("typedEvent") if isinstance(envelope, dict) else None
        if not isinstance(typed, dict):
            typed = create_runtime_event({"event": envelope.get("event"), "data": envelope.get("data") or {}})
        self.write(typed)

    def emit(self, event: str, data: dict[str, Any], *, default_session_id: str | None = None) -> dict[str, Any]:
        typed = create_runtime_event({"event": event, "data": data, "defaultSessionId": default_session_id})
        self.write(typed)
        return typed

    def write(self, envelope: dict[str, Any]) -> None:
        safe = self._redact(sanitize_media_for_persistence(envelope))
        self._observe(safe)
        self.event_count += 1
        if self.stream is None:
            return
        self.stream.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()

    def _observe(self, envelope: dict[str, Any]) -> None:
        if envelope.get("event") != "permission.decided":
            return
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        decision = data.get("decision") or payload.get("decision")
        if decision == "deny":
            self.permission_denied = True

    def _redact(self, value: Any, *, parent_key: str = "", image_context: bool = False) -> Any:
        if isinstance(value, list):
            return [self._redact(item, parent_key=parent_key, image_context=image_context) for item in value]
        if isinstance(value, str):
            return self._redact_string(value, parent_key=parent_key, image_context=image_context)
        if not isinstance(value, dict):
            return value
        block_type = str(value.get("type") or "").strip().lower()
        next_image_context = image_context or block_type in {"image", "input_image", "image_url", "input_file"}
        return {
            str(key): self._redact(item, parent_key=str(key), image_context=next_image_context)
            for key, item in value.items()
        }

    def _redact_string(self, value: str, *, parent_key: str, image_context: bool) -> str:
        key = parent_key.strip().lower()
        if image_context and key in {"data", "data_base64", "base64", "image_url", "url"}:
            return "<redacted-media-payload>"
        if _looks_like_large_data_url(value):
            return "<redacted-data-url>"
        if len(value) <= self.max_string_chars:
            return value
        return value[: self.max_string_chars] + f"...<truncated {len(value) - self.max_string_chars} chars>"


def _looks_like_large_data_url(value: str) -> bool:
    return len(value) > 1024 and value.startswith("data:") and ";base64," in value[:128]
