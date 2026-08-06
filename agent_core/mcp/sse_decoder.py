from __future__ import annotations

from .transport_policy import MCP_SSE_EVENT_MAX_BYTES, McpTransportError


class McpSseDecoder:
    def __init__(self, maxEventBytes: int = MCP_SSE_EVENT_MAX_BYTES):
        self.max_event_bytes = maxEventBytes
        self.line = bytearray()
        self.event_name: str | None = None
        self.data_parts: list[str] = []
        self.event_bytes = 0
        self.closed = False

    def push(
        self,
        value: bytes | bytearray | memoryview,
        *,
        final: bool = False,
    ) -> list[dict[str, str]]:
        if self.closed:
            raise McpTransportError("transport_closed", "MCP SSE decoder is closed.")
        chunk = bytes(value)
        events: list[dict[str, str]] = []
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            end = len(chunk) if newline < 0 else newline
            segment = chunk[offset:end]
            prospective_line = len(self.line) + len(segment)
            normalized = prospective_line
            last = segment[-1] if segment else self.line[-1] if self.line else None
            if last == 0x0D:
                normalized -= 1
            prospective_event = (
                self.event_bytes + normalized
                if newline < 0 or normalized == 0
                else self.event_bytes + normalized + 1
            )
            if prospective_event > self.max_event_bytes:
                raise McpTransportError(
                    "event_limit",
                    f"MCP SSE event exceeds {self.max_event_bytes} bytes.",
                )
            self.line.extend(segment)
            if newline < 0:
                break
            event = self._consume_line(bytes(self.line))
            self.line.clear()
            if event:
                events.append(event)
            offset = newline + 1
        if final:
            if self.line:
                event = self._consume_line(bytes(self.line))
                self.line.clear()
                if event:
                    events.append(event)
            event = self._consume_line(b"")
            if event:
                events.append(event)
            self.closed = True
        return events

    def _consume_line(self, raw: bytes) -> dict[str, str] | None:
        line = raw[:-1] if raw.endswith(b"\r") else raw
        if not line:
            event: dict[str, str] = {}
            if self.event_name is not None:
                event["event"] = self.event_name
            if self.data_parts:
                event["data"] = "\n".join(self.data_parts)
            self.event_name = None
            self.data_parts = []
            self.event_bytes = 0
            return event or None
        self.event_bytes += len(line) + 1
        if self.event_bytes > self.max_event_bytes:
            raise McpTransportError(
                "event_limit",
                f"MCP SSE event exceeds {self.max_event_bytes} bytes.",
            )
        if line.startswith(b":"):
            return None
        try:
            text = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise McpTransportError(
                "invalid_encoding",
                "MCP SSE event is not valid UTF-8.",
            ) from error
        separator = text.find(":")
        field = text if separator < 0 else text[:separator]
        content = "" if separator < 0 else text[separator + 1 :]
        if content.startswith(" "):
            content = content[1:]
        if field == "event":
            self.event_name = content
        elif field == "data":
            self.data_parts.append(content)
        return None


def parseSseEvents(value: bytes | str) -> list[dict[str, str]]:
    decoder = McpSseDecoder()
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return decoder.push(raw, final=True)
