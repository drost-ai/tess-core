from __future__ import annotations

from .shared import *


MCP_HEADER_MAX_BYTES = 16 * 1024
MCP_FRAME_MAX_BYTES = 8 * 1024 * 1024
MCP_DECODED_BODY_MAX_BYTES = 16 * 1024 * 1024
MCP_SSE_EVENT_MAX_BYTES = 1024 * 1024
MCP_NOTIFICATION_QUEUE_MAX = 256
MCP_PENDING_REQUEST_MAX = 256
MCP_STDERR_TAIL_MAX_BYTES = 16 * 1024

MCP_DEFAULT_STARTUP_TIMEOUT_MS = 10_000
MCP_DEFAULT_REQUEST_TIMEOUT_MS = 30_000
MCP_DEFAULT_CONNECT_TIMEOUT_MS = 10_000
MCP_DEFAULT_HEADERS_TIMEOUT_MS = 10_000
MCP_DEFAULT_BODY_IDLE_TIMEOUT_MS = 10_000
MCP_CLOSE_GRACE_MS = 750
MCP_CLOSE_STDIN_GRACE_MS = 100
MCP_CLOSE_KILL_WAIT_MS = 2_000


class McpTransportError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.name = "McpTransportError"
        self.code = code


def resolveMcpTransportPolicy(config: dict[str, Any] | None = None) -> dict[str, int]:
    values = config or {}
    return {
        "startupTimeoutMs": _positive_timeout(
            values.get("startupTimeoutMs"),
            values.get("startupTimeoutSeconds"),
            MCP_DEFAULT_STARTUP_TIMEOUT_MS,
        ),
        "requestTimeoutMs": _positive_timeout(
            values.get("requestTimeoutMs"),
            values.get("requestTimeoutSeconds"),
            MCP_DEFAULT_REQUEST_TIMEOUT_MS,
        ),
        "connectTimeoutMs": _positive_timeout(
            values.get("connectTimeoutMs"),
            None,
            MCP_DEFAULT_CONNECT_TIMEOUT_MS,
        ),
        "headersTimeoutMs": _positive_timeout(
            values.get("headersTimeoutMs"),
            None,
            MCP_DEFAULT_HEADERS_TIMEOUT_MS,
        ),
        "bodyIdleTimeoutMs": _positive_timeout(
            values.get("bodyIdleTimeoutMs"),
            values.get("streamTimeoutSeconds"),
            MCP_DEFAULT_BODY_IDLE_TIMEOUT_MS,
        ),
    }


def _positive_timeout(value_ms: Any, legacy_seconds: Any, fallback: int) -> int:
    if isinstance(value_ms, int) and not isinstance(value_ms, bool) and value_ms > 0:
        return min(600_000, value_ms)
    if (
        isinstance(legacy_seconds, (int, float))
        and not isinstance(legacy_seconds, bool)
        and math.isfinite(float(legacy_seconds))
        and legacy_seconds > 0
    ):
        return min(600_000, max(1, int(float(legacy_seconds) * 1000)))
    return fallback
