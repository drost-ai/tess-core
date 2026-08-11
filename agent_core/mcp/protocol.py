from __future__ import annotations
from .._utils import drop_none as _without_none_values

import json
import re
from typing import Any

from .transport_shared import _reject_json_constant
from .sse_decoder import parseSseEvents


def _runtime_tool_name(server: str, tool: str) -> str:
    return f"mcp__{_sanitize_name(server)}__{_sanitize_name(tool)}"


def _sanitize_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or "item"


class McpProtocolError(RuntimeError):
    """A JSON-RPC error response from a live server; the transport is healthy."""

    def __init__(self, message: str, code: Any = None, data: Any = None):
        super().__init__(message)
        self.name = "McpProtocolError"
        self.code = code
        self.data = data


def _unwrap_json_rpc_response(response: dict[str, Any]) -> Any:
    if response.get("error"):
        error = response["error"]
        if isinstance(error, dict):
            raise McpProtocolError(
                f"{error.get('code')}: {error.get('message')}",
                code=error.get("code"),
                data=error.get("data"),
            )
        raise McpProtocolError(str(error))
    if "result" in response:
        return response.get("result")
    return None
