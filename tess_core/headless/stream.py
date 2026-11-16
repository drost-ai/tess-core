from __future__ import annotations

import json
from typing import Any, TextIO

from ..payloads.media_persistence import sanitize_media_for_persistence
from ..runtime.events import create_runtime_event


class JsonStreamSink:
    def __init__(self, stream: TextIO | None = None, *, max_string_chars: int = 12000) -> None:
        self.stream = stream
        self.max_string_chars = max_string_chars
        self.denied_calls = 0
        self._blocked_tools: set[str] = set()
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

    @property
    def permission_denied(self) -> bool:
        """A refusal the run never got past: a tool that was refused and did
        not then run successfully. A model that is refused one spelling of a
        command and succeeds with another has not been stopped."""
        return bool(self._blocked_tools)

    def _observe(self, envelope: dict[str, Any]) -> None:
        event = envelope.get("event")
        if event not in ("permission.decided", "tool.completed"):
            return
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        if event == "permission.decided":
            if (data.get("decision") or payload.get("decision")) == "deny":
                self.denied_calls += 1
                self._blocked_tools.add(str(data.get("tool") or payload.get("tool") or ""))
        elif not (data.get("error") or payload.get("error")):
            self._blocked_tools.discard(str(data.get("name") or payload.get("name") or ""))

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
