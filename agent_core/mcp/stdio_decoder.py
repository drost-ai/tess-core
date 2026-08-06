from __future__ import annotations

import re

from .transport_policy import MCP_FRAME_MAX_BYTES, MCP_HEADER_MAX_BYTES, McpTransportError


_HEADER_DELIMITER = b"\r\n\r\n"


class McpStdioFrameDecoder:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.expected_length: int | None = None

    @property
    def bufferedBytes(self) -> int:
        return len(self.buffer)

    def push(self, value: bytes | bytearray | memoryview) -> list[str]:
        chunk = bytes(value)
        if not chunk:
            return []
        frames: list[str] = []
        offset = 0
        while offset < len(chunk):
            self._drain(frames)
            retained_limit = (
                MCP_HEADER_MAX_BYTES + len(_HEADER_DELIMITER)
                if self.expected_length is None
                else self.expected_length
            )
            capacity = retained_limit - len(self.buffer)
            if capacity <= 0:
                raise McpTransportError(
                    "invalid_framing",
                    "MCP stdio decoder made no framing progress.",
                )
            admitted = min(capacity, len(chunk) - offset)
            self.buffer.extend(chunk[offset : offset + admitted])
            offset += admitted
        self._drain(frames)
        return frames

    def reset(self) -> None:
        self.buffer.clear()
        self.expected_length = None

    def _drain(self, frames: list[str]) -> None:
        while self.buffer or self.expected_length == 0:
            if self.expected_length is None:
                delimiter = self.buffer.find(_HEADER_DELIMITER)
                if delimiter < 0:
                    if len(self.buffer) > MCP_HEADER_MAX_BYTES:
                        raise McpTransportError(
                            "header_limit",
                            f"MCP stdio header exceeds {MCP_HEADER_MAX_BYTES} bytes.",
                        )
                    return
                if delimiter > MCP_HEADER_MAX_BYTES:
                    raise McpTransportError(
                        "header_limit",
                        f"MCP stdio header exceeds {MCP_HEADER_MAX_BYTES} bytes.",
                    )
                self.expected_length = _parse_content_length(bytes(self.buffer[:delimiter]))
                del self.buffer[: delimiter + len(_HEADER_DELIMITER)]
            if len(self.buffer) < self.expected_length:
                return
            payload = bytes(self.buffer[: self.expected_length])
            del self.buffer[: self.expected_length]
            self.expected_length = None
            try:
                frames.append(payload.decode("utf-8", errors="strict"))
            except UnicodeDecodeError as error:
                raise McpTransportError(
                    "invalid_encoding",
                    "MCP stdio frame is not valid UTF-8.",
                ) from error


def _parse_content_length(header: bytes) -> int:
    if any(byte > 0x7F or (byte < 0x20 and byte not in (0x09, 0x0A, 0x0D)) for byte in header):
        raise McpTransportError(
            "invalid_encoding",
            "MCP stdio header is not valid ASCII.",
        )
    lengths: list[str] = []
    for line in header.decode("ascii").split("\r\n"):
        separator = line.find(":")
        if separator <= 0:
            raise McpTransportError(
                "invalid_framing",
                "MCP stdio header contains a malformed field.",
            )
        name = line[:separator].strip().lower()
        field = line[separator + 1 :].strip()
        if name == "content-length":
            lengths.append(field)
    if len(lengths) != 1 or not re.fullmatch(r"(?:0|[1-9]\d*)", lengths[0]):
        raise McpTransportError(
            "invalid_framing",
            "MCP stdio frame requires exactly one finite non-negative Content-Length.",
        )
    length = int(lengths[0])
    if length > MCP_FRAME_MAX_BYTES:
        raise McpTransportError(
            "frame_limit",
            f"MCP stdio Content-Length exceeds {MCP_FRAME_MAX_BYTES} bytes.",
        )
    return length
