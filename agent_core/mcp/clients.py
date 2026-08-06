from __future__ import annotations

import threading
from typing import Any, Callable

from .negotiation import (
    PREFERRED_PROTOCOL_VERSION,
    bounded_capabilities,
    resolve_negotiated_protocol,
)
from .protocol import _unwrap_json_rpc_response


class JsonRpcClient:
    def __init__(
        self,
        config: dict[str, Any],
        cwd: str,
        on_notification: Callable[[dict[str, Any]], Any] | None = None,
        auth: Any = None,
    ) -> None:
        self.config = config
        self.cwd = cwd
        self.on_notification = on_notification
        self.next_id = 1
        self._id_lock = threading.Lock()
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.negotiated_protocol_version: str | None = None
        self.auth = auth

    async def initialize(self) -> None:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PREFERRED_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tess", "version": "0.1.0"},
            },
        )
        if isinstance(result, dict):
            self.server_info = bounded_capabilities(result.get("serverInfo"))
            self.server_capabilities = bounded_capabilities(result.get("capabilities"))
        self.negotiated_protocol_version = resolve_negotiated_protocol(result)
        await self.notify("notifications/initialized")

    def _http_headers(self, base: dict[str, str]) -> dict[str, str]:
        headers = {**base, **(self.config.get("headers") or {})}
        if self.negotiated_protocol_version:
            headers["MCP-Protocol-Version"] = self.negotiated_protocol_version
        return headers

    async def request(self, method: str, params: Any = None) -> Any:
        with self._id_lock:
            request_id = self.next_id
            self.next_id += 1
        response = await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return _unwrap_json_rpc_response(response)

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        return None

    async def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class _McpHttpUnauthorized(RuntimeError):
    def __init__(self, www_authenticate: str | None):
        super().__init__("MCP HTTP request requires authorization.")
        self.www_authenticate = www_authenticate
