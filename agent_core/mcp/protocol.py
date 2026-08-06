from __future__ import annotations

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


def _unwrap_json_rpc_response(response: dict[str, Any]) -> Any:
    if response.get("error"):
        error = response["error"]
        if isinstance(error, dict):
            raise RuntimeError(f"{error.get('code')}: {error.get('message')}")
        raise RuntimeError(str(error))
    if "result" in response:
        return response.get("result")
    return None


def _without_none_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}










