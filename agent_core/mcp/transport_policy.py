from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knob_value

    return knob_value(name)


import math
from typing import Any


MCP_HEADER_MAX_BYTES = 16 * 1024
MCP_FRAME_MAX_BYTES = 8 * 1024 * 1024
MCP_DECODED_BODY_MAX_BYTES = 16 * 1024 * 1024
MCP_SSE_EVENT_MAX_BYTES = 1024 * 1024
MCP_NOTIFICATION_QUEUE_MAX = 256
MCP_PENDING_REQUEST_MAX = 256
MCP_STDERR_TAIL_MAX_BYTES = 16 * 1024



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
            int(_knob("mcp.startup_timeout_ms")),
        ),
        "requestTimeoutMs": _positive_timeout(
            values.get("requestTimeoutMs"),
            values.get("requestTimeoutSeconds"),
            int(_knob("mcp.request_timeout_ms")),
        ),
        "connectTimeoutMs": _positive_timeout(
            values.get("connectTimeoutMs"),
            None,
            int(_knob("mcp.connect_timeout_ms")),
        ),
        "headersTimeoutMs": _positive_timeout(
            values.get("headersTimeoutMs"),
            None,
            int(_knob("mcp.headers_timeout_ms")),
        ),
        "bodyIdleTimeoutMs": _positive_timeout(
            values.get("bodyIdleTimeoutMs"),
            values.get("streamTimeoutSeconds"),
            int(_knob("mcp.body_idle_timeout_ms")),
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
