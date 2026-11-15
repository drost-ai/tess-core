"""Host-owned, opt-in MCP connection multiplexing for resident sessions.

The host alone owns admission (``connect``) and shutdown. A child receives only a
revocable client capability, never the underlying transport or another grant.
This is an in-process transport boundary, not a sandbox against arbitrary Python
execution in the host. No listening socket or ambient/global registry is used.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

from .clients import JsonRpcClient
from .auth import create_mcp_oauth_runtime
from .token_store import create_file_mcp_token_store
from ..runtime.secrets import resolve_mcp_server_config_secrets
from .http_transport import HttpMcpClient
from .sse_transport import SseMcpClient
from .stdio_transport import StdioMcpClient


@dataclass(frozen=True)
class BrokerGrant:
    """Pinned host-authorized raw MCP names/URIs; empty means deny; None is an explicit full-authority host grant."""

    child_id: str
    tools: frozenset[str] | None = frozenset()
    resources: frozenset[str] | None = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.child_id, str) or not self.child_id:
            raise ValueError("A broker grant requires a child identity")
        for name in ("tools", "resources"):
            values = getattr(self, name)
            if values is None:
                continue
            if isinstance(values, str) or not all(isinstance(v, str) and v for v in values):
                raise ValueError(f"Invalid broker grant {name}")
            object.__setattr__(self, name, frozenset(values))


def is_host_shareable(descriptor: dict[str, Any]) -> bool:
    return (
        descriptor.get("sharing") == {"mode": "host"}
        and descriptor.get("delegation") == {"mode": "allow", "scope": "shared"}
    )


def source_key(descriptor: dict[str, Any], cwd: str, source_fingerprint: str) -> str:
    if not isinstance(source_fingerprint, str) or not source_fingerprint:
        raise ValueError("Sharing requires a pinned source fingerprint")
    encoded = json.dumps([descriptor, cwd, source_fingerprint], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _backend(descriptor: dict[str, Any], cwd: str) -> JsonRpcClient:
    transport = descriptor.get("transport", "stdio")
    classes = {"stdio": StdioMcpClient, "http": HttpMcpClient, "sse": SseMcpClient}
    if transport not in classes:
        raise ValueError("Unsupported shared MCP transport")
    if transport == "stdio":
        return StdioMcpClient(descriptor, cwd)
    auth = create_mcp_oauth_runtime(config=descriptor, store=create_file_mcp_token_store(),
                                    on_runtime_event=lambda *_: None, authorization_callback=None)
    return classes[transport](descriptor, cwd, auth=auth)


@dataclass
class _Lease:
    key: str
    grant: BrokerGrant
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    children: set[str] = field(default_factory=set)


class SharedMcpBroker:
    """One owner/event loop, one initialized transport per source identity.

    ``client_factory`` is a synchronous host factory(config, cwd) returning an
    uninitialized JsonRpcClient. Supply it for host-managed OAuth, if required.
    Admission must be invoked by trusted host wiring after authority compilation.
    Children must never receive this owner object through tool arguments/config.
    """

    def __init__(self, client_factory: Callable[..., JsonRpcClient] | None = None) -> None:
        self._factory = client_factory or _backend
        self._clients: dict[str, JsonRpcClient] = {}
        self._leases: dict[str, _Lease] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def connect(self, descriptor: dict[str, Any], *, cwd: str,
                      source_fingerprint: str, grant: BrokerGrant) -> SharedMcpClient:
        descriptor = copy.deepcopy(descriptor)
        if not is_host_shareable(descriptor):
            raise PermissionError("MCP descriptor is not explicitly host-shareable")
        if not isinstance(grant, BrokerGrant):
            raise TypeError("A pinned BrokerGrant is required")
        key = source_key(descriptor, cwd, source_fingerprint)
        async with self._lock:
            if self._closed:
                raise RuntimeError("MCP broker is closed")
            if key not in self._clients:
                resolved = await resolve_mcp_server_config_secrets(descriptor)
                client = self._factory(resolved, cwd)
                try:
                    await client.initialize()
                except BaseException:
                    await client.close()
                    raise
                self._clients[key] = client
            token = secrets.token_urlsafe(32)
            self._leases[token] = _Lease(key, grant)
        return SharedMcpClient(self, token, descriptor.get("name", "shared"), cwd)

    def _authenticate(self, token: str) -> _Lease:
        lease = self._leases.get(token) if isinstance(token, str) else None
        if self._closed or lease is None:
            raise PermissionError("Invalid or revoked MCP capability")
        return lease

    async def request(self, token: str, method: str, params: Any = None) -> Any:
        lease = self._authenticate(token)
        params = copy.deepcopy(params or {})
        if not isinstance(params, dict):
            raise ValueError("MCP params must be an object")
        client = self._clients[lease.key]
        if method == "initialize":
            return {"protocolVersion": client.negotiated_protocol_version,
                    "serverInfo": copy.deepcopy(client.server_info),
                    "capabilities": {"tools": {}, "resources": {}}}
        if method == "ping":
            return {}
        if method == "tools/call":
            if lease.grant.tools is not None and params.get("name") not in lease.grant.tools:
                raise PermissionError("MCP tool is outside the child grant")
        elif method == "resources/read":
            if lease.grant.resources is not None and params.get("uri") not in lease.grant.resources:
                raise PermissionError("MCP resource is outside the child grant")
        elif method not in ("tools/list", "resources/list"):
            raise PermissionError("MCP method is not broker-authorized")
        # Distinct transport requests receive distinct IDs. Cancelling this task
        # never cancels a sibling task or closes the shared backend.
        task = asyncio.create_task(client.request(method, params))
        lease.tasks.add(task)
        try:
            result = await task
            self._authenticate(token)  # revocation also gates delivery
            if method in ("tools/list", "resources/list"):
                result = copy.deepcopy(result)
                field_name, identity, allowed = (
                    ("tools", "name", lease.grant.tools) if method == "tools/list"
                    else ("resources", "uri", lease.grant.resources)
                )
                result[field_name] = [item for item in result.get(field_name, [])
                                      if allowed is None or item.get(identity) in allowed]
            return result
        finally:
            lease.tasks.discard(task)

    async def revoke(self, token: str) -> None:
        await self._revoke_roots([token])

    async def revoke_child(self, child_id: str) -> None:
        """Trusted host terminal observer API; never exposed as remote RPC.

        Invalidate every lease for this child and all descendants before the
        first await, including leases minted across session reconnects.
        """
        roots = [token for token, lease in self._leases.items()
                 if lease.grant.child_id == child_id]
        await self._revoke_roots(roots)

    async def _revoke_roots(self, roots: list[str]) -> None:
        pending = list(roots)
        tasks = set()
        while pending:
            lease = self._leases.pop(pending.pop(), None)
            if lease:
                pending.extend(lease.children)
                tasks.update(lease.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            for token in list(self._leases):
                await self.revoke(token)
            clients, self._clients = list(self._clients.values()), {}
            results = await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
            failures = [result for result in results if isinstance(result, Exception)]
            if failures:
                raise ExceptionGroup("Shared MCP shutdown failed", failures)


class SharedMcpClient(JsonRpcClient):
    """Session-owned revocable adapter. Closing it does not close the service."""

    def __init__(self, broker: SharedMcpBroker, token: str, name: str, cwd: str) -> None:
        # The session-visible config contains neither backend secrets nor token.
        super().__init__({"name": name, "transport": "host-shared"}, cwd)
        self.__broker = broker
        self.__token = token

    async def request(self, method: str, params: Any = None) -> Any:
        return await self.__broker.request(self.__token, method, params)

    async def notify(self, method: str, params: Any = None) -> None:
        self.__broker._authenticate(self.__token)
        if method != "notifications/initialized":
            raise PermissionError("Child notifications cannot affect a shared MCP service")

    async def close(self) -> None:
        await self.__broker.revoke(self.__token)
