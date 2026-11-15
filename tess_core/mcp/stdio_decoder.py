from __future__ import annotations

from .transport_policy import MCP_FRAME_MAX_BYTES, McpTransportError


class McpStdioFrameDecoder:
    """Newline-delimited JSON-RPC decoder for the MCP stdio transport.

    One message per line per the MCP spec; a trailing carriage return is
    stripped and blank lines are skipped. The retained partial line is
    bounded by MCP_FRAME_MAX_BYTES so a server that never sends a newline
    cannot grow the buffer without limit.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()

    @property
    def bufferedBytes(self) -> int:
        return len(self.buffer)

    def push(self, value: bytes | bytearray | memoryview) -> list[str]:
        chunk = bytes(value)
        frames: list[str] = []
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            if newline < 0:
                self._admit(chunk[offset:])
                break
            self._admit(chunk[offset:newline])
            offset = newline + 1
            line = bytes(self.buffer)
            self.buffer.clear()
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                continue
            try:
                frames.append(line.decode("utf-8", errors="strict"))
            except UnicodeDecodeError as error:
                raise McpTransportError(
                    "invalid_encoding",
                    "MCP stdio frame is not valid UTF-8.",
                ) from error
        return frames

    def reset(self) -> None:
        self.buffer.clear()

    def _admit(self, part: bytes) -> None:
        if len(self.buffer) + len(part) > MCP_FRAME_MAX_BYTES:
            raise McpTransportError(
                "frame_limit",
                f"MCP stdio message exceeds {MCP_FRAME_MAX_BYTES} bytes.",
            )
        self.buffer.extend(part)
