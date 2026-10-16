"""Private authenticated loopback MCP facade for subprocess session clients.

Returned descriptors contain opaque references to the existing private runtime
secret store, never plaintext bearer credentials.
No browser origin is accepted. Tokens authorize exactly one immutable grant.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from urllib.parse import urlsplit
from typing import Any

from .shared_broker import BrokerGrant, SharedMcpBroker as _Core, _Lease
from .protocol import _runtime_tool_name
from ..runtime.secrets import (authorize_runtime_secret_write, store_runtime_secret,
                               resolve_mcp_server_config_secrets, resolve_runtime_secret_string)

_MAX_BODY = 4 * 1024 * 1024


class SharedMcpBroker(_Core):
    def __init__(self, client_factory: Any = None) -> None:
        super().__init__(client_factory)
        self._listener: asyncio.Server | None = None
        self._connections: set[asyncio.Task[Any]] = set()
        self._host_header = ""
        self._stopping = False

    async def start(self) -> None:
        async with self._lock:
            if self._closed or self._stopping:
                raise RuntimeError("MCP broker is closed")
            if self._listener is None:
                self._listener = await asyncio.start_server(self._serve, "127.0.0.1", 0, limit=16384)
                port = self._listener.sockets[0].getsockname()[1]
                self._host_header = f"127.0.0.1:{port}"

    async def acquire_descriptor(self, server_config: dict[str, Any], *, cwd: str,
                                 source_fingerprint: str, grant: BrokerGrant) -> dict[str, Any]:
        if self._listener is None or self._stopping:
            raise RuntimeError("Start the host MCP broker before admission")
        # Admission lives only in the host API; it is deliberately not exposed
        # over HTTP. The remote bearer cannot mint or widen another grant.
        client = await self.connect(server_config, cwd=cwd, source_fingerprint=source_fingerprint, grant=grant)
        token = client._SharedMcpClient__token
        try:
            return await self._descriptor(token, server_config["name"])
        except BaseException:
            await self.revoke(token)
            raise

    async def _descriptor(self, token: str, name: str) -> dict[str, Any]:
        key = "shared-mcp:" + secrets.token_hex(32)
        reference = await store_runtime_secret(
            purpose="mcp_header", secret=("Bearer " + token).encode(),
            idempotencyKey=key,
            authorization=authorize_runtime_secret_write(purpose="mcp_header", idempotencyKey=key),
        )
        return {"name": name, "transport": "http", "commandOrUrl": f"http://{self._host_header}/mcp",
                "headers": {"Authorization": reference},
                "delegation": {"mode": "allow", "scope": "shared"},
                "sharedBroker": {"version": 1}}

    async def revoke_descriptor(self, descriptor: dict[str, Any]) -> None:
        authorization = await resolve_runtime_secret_string(descriptor.get("headers", {}).get("Authorization", ""), "mcp_header")
        if authorization.startswith("Bearer "):
            await self.revoke(authorization[7:])

    async def request(self, token: str, method: str, params: Any = None) -> Any:
        if method == "broker/revoke":
            self._authenticate(token)
            await self.revoke(token)
            return {}
        if method != "broker/delegate":
            return await super().request(token, method, params)
        parent = self._authenticate(token)
        if not isinstance(params, dict) or set(params) not in ({"child_id", "tools", "resources"}, {"child_id", "tools", "resources", "enabled_tools"}):
            raise PermissionError("Invalid delegation request")
        tools = params["tools"]
        if "enabled_tools" in params:
            enabled = params["enabled_tools"]
            if not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled):
                raise PermissionError("Invalid runtime tool scope")
            selected: set[str] = set()
            cursor, seen = None, set()
            while True:
                catalog = await super().request(token, "tools/list", {"cursor": cursor} if cursor else {})
                name = self._clients[parent.key].config["name"]
                selected.update(item["name"] for item in catalog.get("tools", [])
                                if _runtime_tool_name(name, item["name"]) in enabled)
                cursor = catalog.get("nextCursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise RuntimeError("Cyclic MCP catalog pagination")
                seen.add(cursor)
            tools = selected if tools is None else selected.intersection(tools)
        grant = BrokerGrant(params["child_id"], tools, params["resources"])
        if any(ceiling is not None and (requested is None or not requested <= ceiling)
               for requested, ceiling in ((grant.tools, parent.grant.tools), (grant.resources, parent.grant.resources))):
            raise PermissionError("Delegation cannot widen MCP authority")
        child_token = secrets.token_urlsafe(32)
        self._leases[child_token] = _Lease(parent.key, grant)
        parent.children.add(child_token)
        try:
            descriptor = await self._descriptor(child_token, self._clients[parent.key].config["name"])
            self._authenticate(token)
            return descriptor
        except BaseException:
            await self.revoke(child_token)
            raise

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        self._connections.add(current)
        request_task = None
        disconnect = None
        try:
            status, payload = 400, {"error": "Malformed request"}
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = raw.decode("ascii").split("\r\n")
            method, path, version = lines[0].split(" ")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                if key in headers:
                    raise ValueError("Duplicate header")
                headers[key] = value.strip()
            if headers.get("host") != self._host_header or "origin" in headers:
                status, payload = 403, {"error": "Forbidden origin"}
            elif method != "POST" or path != "/mcp" or version != "HTTP/1.1":
                status, payload = 405, {"error": "Only MCP POST is supported"}
            else:
                auth = headers.get("authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                try:
                    self._authenticate(token)
                except PermissionError:
                    status, payload = 401, {"error": "Invalid capability"}
                else:
                    if "transfer-encoding" in headers or headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                        raise ValueError("Expected bounded JSON body")
                    length = int(headers.get("content-length", "-1"))
                    if not 0 <= length <= _MAX_BODY:
                        raise ValueError("Invalid body size")
                    body = await asyncio.wait_for(reader.readexactly(length), 30)
                    message = json.loads(body)
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
                        raise ValueError("Expected one JSON-RPC request")
                    if "id" not in message:
                        if message["method"] != "notifications/initialized":
                            raise PermissionError("Notification is not authorized")
                        status, payload = 202, None
                    else:
                        request_task = asyncio.create_task(self.request(token, message["method"], message.get("params")))
                        # TCP disconnect cancels only this request's upstream task.
                        disconnect = asyncio.create_task(reader.read(1))
                        done, _ = await asyncio.wait((request_task, disconnect), return_when=asyncio.FIRST_COMPLETED)
                        if request_task not in done:
                            request_task.cancel()
                            return
                        try:
                            result = await request_task
                            payload = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                        except PermissionError:
                            payload = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32600, "message": "Outside authorized MCP grant"}}
                        except Exception:
                            payload = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": "Shared MCP request failed"}}
                        status = 200
            await self._reply(writer, status, payload)
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, PermissionError, ConnectionError):
            with contextlib.suppress(Exception):
                await self._reply(writer, 400, {"error": "Invalid MCP request"})
        finally:
            for task in (request_task, disconnect):
                if task is not None:
                    task.cancel()
            draining = [t for t in (request_task, disconnect) if t is not None]
            if draining:
                await asyncio.wait(draining, timeout=1.0)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self._connections.discard(current)

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode() if payload is not None else b""
        reasons = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 405: "Method Not Allowed"}
        writer.write((f"HTTP/1.1 {status} {reasons[status]}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n").encode() + body)
        await writer.drain()

    async def close(self) -> None:
        self._stopping = True
        if self._listener is not None:
            self._listener.close()
            await self._listener.wait_closed()
            self._listener = None
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()


async def delegate_shared_mcp_descriptor(descriptor: dict[str, Any], grant: BrokerGrant, *,
                                         enabled_tools: frozenset[str] | None = None) -> dict[str, Any]:
    """Narrow a persisted secret-reference descriptor in a subprocess host."""
    resolved = await resolve_mcp_server_config_secrets(descriptor)
    params = {"child_id": grant.child_id,
              "tools": sorted(grant.tools) if grant.tools is not None else None,
              "resources": sorted(grant.resources) if grant.resources is not None else None}
    if enabled_tools is not None:
        params["enabled_tools"] = sorted(enabled_tools)
    return await _control_request(resolved, "broker/delegate", params)


async def revoke_shared_mcp_descriptor(descriptor: dict[str, Any]) -> None:
    await _control_request(await resolve_mcp_server_config_secrets(descriptor), "broker/revoke", {})


async def _control_request(descriptor: dict[str, Any], method: str, params: dict[str, Any]) -> Any:
    # Control RPC uses async sockets: occupying the default executor with local
    # HTTP waits can starve the private secret store during 32-way admission.
    url = urlsplit(descriptor["commandOrUrl"])
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.path != "/mcp" or url.query or url.fragment or url.username:
        raise PermissionError("Shared MCP control requires a private loopback endpoint")
    async with asyncio.timeout(30):
        reader, writer = await asyncio.open_connection(url.hostname, url.port)
        try:
            body = json.dumps({"jsonrpc":"2.0", "id":1, "method":method, "params":params}).encode()
            auth = descriptor["headers"]["Authorization"]
            if not isinstance(auth, str) or "\r" in auth or "\n" in auth:
                raise ValueError("Invalid broker authorization")
            writer.write((f"POST /mcp HTTP/1.1\r\nHost: {url.netloc}\r\nAuthorization: {auth}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
            await writer.drain()
            headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            if headers[0].split()[1] != "200":
                raise PermissionError("Shared MCP control rejected")
            length = next(int(line.split(":",1)[1]) for line in headers if line.lower().startswith("content-length:"))
            if not 0 <= length <= _MAX_BODY:
                raise ValueError("Invalid control response size")
            response = json.loads(await reader.readexactly(length))
            if "error" in response:
                raise PermissionError("Shared MCP control outside authorized grant")
            return response["result"]
        finally:
            writer.close()
            await writer.wait_closed()
