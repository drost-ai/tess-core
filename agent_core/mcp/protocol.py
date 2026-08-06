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


def _parse_http_rpc_response(status: int, raw: str, content_type: str, request_id: int | None) -> dict[str, Any]:
    if status in (202, 204):
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if "text/event-stream" in content_type:
        return _parse_sse_rpc_response(raw, request_id)
    if not raw.strip():
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise RuntimeError("Invalid JSON-RPC response from MCP server.") from exc
    return _validate_json_rpc_response(payload, request_id)


def _parse_sse_rpc_response(raw: str, request_id: int | None) -> dict[str, Any]:
    for event in _parse_sse_events(raw):
        data = event.get("data")
        if not data:
            continue
        try:
            payload = json.loads(data, parse_constant=_reject_json_constant)
        except Exception as exc:
            raise RuntimeError("MCP SSE event contains malformed JSON-RPC.") from exc
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise RuntimeError("MCP SSE event contains malformed JSON-RPC.")
        if isinstance(payload, dict) and _json_rpc_id_matches(payload, request_id):
            return _validate_json_rpc_response(payload, request_id, id_already_matched=True)
    raise RuntimeError("No matching SSE JSON-RPC response was received.")


def _validate_json_rpc_response(payload: Any, request_id: int | None, id_already_matched: bool = False) -> dict[str, Any]:
    if isinstance(payload, list):
        raise RuntimeError("MCP server returned a batch JSON-RPC response.")
    if not isinstance(payload, dict):
        raise RuntimeError("MCP server returned an invalid JSON-RPC response.")
    if request_id is not None and not id_already_matched:
        if "id" not in payload:
            raise RuntimeError("MCP server response missing JSON-RPC id.")
        if not _json_rpc_id_matches(payload, request_id):
            raise RuntimeError("MCP server response id did not match the pending request.")
    if "error" in payload or "result" in payload:
        return payload
    if request_id is None and payload.get("method"):
        return {"jsonrpc": "2.0", "id": None, "result": {}}
    raise RuntimeError("MCP server returned an invalid JSON-RPC response.")


def _json_rpc_id_matches(payload: dict[str, Any], request_id: int | None) -> bool:
    if request_id is None:
        return "id" not in payload
    payload_id = payload.get("id")
    return isinstance(payload_id, int) and not isinstance(payload_id, bool) and payload_id == request_id


def _parse_sse_events(raw: str) -> list[dict[str, str]]:
    return parseSseEvents(raw)
