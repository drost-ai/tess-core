from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .transport_policy import MCP_NOTIFICATION_QUEUE_MAX, McpTransportError


class BoundedNotificationQueue:
    def __init__(self, handler: Callable[[dict[str, Any]], Any] | None = None):
        self.handler = handler
        self.queue: list[dict[str, Any]] = []
        self.catalog_signals: set[str] = set()
        self.closed = False
        self.lock = threading.Lock()

    def push(self, notification: dict[str, Any]) -> None:
        method = notification.get("method")
        if not isinstance(method, str):
            return
        catalog_change = _is_catalog_change(method)
        with self.lock:
            if self.closed or not self.handler:
                return
            if catalog_change and method in self.catalog_signals:
                return
            if len(self.queue) >= MCP_NOTIFICATION_QUEUE_MAX:
                if not catalog_change:
                    return
                evict = next(
                    (
                        index
                        for index, entry in enumerate(self.queue)
                        if not _is_catalog_change(str(entry.get("method") or ""))
                    ),
                    None,
                )
                if evict is None:
                    return
                self.queue.pop(evict)
            self.queue.append(notification)
            if catalog_change:
                self.catalog_signals.add(method)

    def flush(self) -> None:
        while True:
            with self.lock:
                if not self.queue:
                    return
                notification = self.queue.pop(0)
                method = notification.get("method")
                if isinstance(method, str):
                    self.catalog_signals.discard(method)
            try:
                if self.handler:
                    self.handler(notification)
            except Exception:
                pass

    def close(self) -> None:
        self.flush()
        with self.lock:
            self.closed = True


def _is_catalog_change(method: str) -> bool:
    return method in {
        "notifications/tools/list_changed",
        "notifications/resources/list_changed",
    }


def _as_json_rpc_record(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _validate_transport_message(value: Any, source: str) -> dict[str, Any]:
    message = _as_json_rpc_record(value)
    if not message or message.get("jsonrpc") != "2.0":
        raise McpTransportError(
            "invalid_framing",
            f"MCP {source} contains malformed JSON-RPC.",
        )
    request_id = message.get("id")
    if request_id is not None and (
        isinstance(request_id, bool) or not isinstance(request_id, (int, str))
    ):
        raise McpTransportError(
            "invalid_framing",
            f"MCP {source} id is not a string or number.",
        )
    # Server-initiated requests (they carry a method) may use any legal id
    # shape; responses settle against our ids, which are always numeric.
    if (
        request_id is not None
        and not isinstance(request_id, int)
        and not isinstance(message.get("method"), str)
    ):
        raise McpTransportError(
            "invalid_framing",
            f"MCP {source} response id is not numeric.",
        )
    return message


def _server_request_reply(request_id: Any, method: str) -> dict[str, Any]:
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _decode_json_rpc_payload(payload: str, source: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload, parse_constant=_reject_json_constant)
    except Exception as error:
        raise McpTransportError(
            "invalid_framing",
            f"Invalid JSON-RPC response in MCP {source}.",
        ) from error
    if isinstance(parsed, list):
        raise McpTransportError(
            "invalid_framing",
            "MCP server returned a batch JSON-RPC response.",
        )
    return _validate_transport_message(parsed, source)


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _transport_error(error: BaseException, fallback: str) -> BaseException:
    if isinstance(error, McpTransportError):
        return error
    return McpTransportError(fallback, str(error))


class contextlib_suppress:
    def __enter__(self):
        return None

    def __exit__(self, *_exc):
        return True
