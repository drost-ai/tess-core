from __future__ import annotations

import asyncio
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .runtime.secrets import resolveMcpServerConfigSecrets


class McpResourceResponse:
    def __init__(self, status: int, body: bytes, headers: Mapping[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = dict(headers or {})
        self.ok = 200 <= status < 300

    async def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _safe_http_url(value: str, field: str):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field} URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"Invalid {field} URL origin")
    default_port = 80 if parsed.scheme == "http" else 443
    origin = f"{parsed.scheme}://{parsed.hostname.lower()}"
    if port is not None and port != default_port:
        origin += f":{port}"
    return parsed, origin


def canonicalMcpOrigin(value: str) -> str:
    return _safe_http_url(value, "configured MCP")[1]


async def fetchMcpResourceWithBoundCredentials(
    args: Mapping[str, Any] | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    uri: str | None = None,
    maxRedirects: int | None = None,
    signal: Any = None,
) -> McpResourceResponse:
    values = dict(args or {})
    server = values.get("config", config)
    resource_uri = values.get("uri", uri)
    if not isinstance(server, Mapping) or server.get("transport") not in {"http", "sse"}:
        raise ValueError("MCP resource fallback requires an HTTP or SSE server")
    if not isinstance(resource_uri, str):
        raise ValueError("Invalid MCP resource URL")
    configured_origin = canonicalMcpOrigin(str(server.get("commandOrUrl")))
    _, current_origin = _safe_http_url(resource_uri, "MCP resource")
    if current_origin != configured_origin:
        raise ValueError("MCP resource origin does not match the configured server")
    resolved = await resolveMcpServerConfigSecrets(server)
    redirect_limit = max(0, min(int(values.get("maxRedirects", maxRedirects) if values.get("maxRedirects", maxRedirects) is not None else 5), 10))
    current = resource_uri
    for redirects in range(redirect_limit + 1):
        _, current_origin = _safe_http_url(current, "MCP resource redirect" if redirects else "MCP resource")
        if current_origin != configured_origin:
            raise ValueError("MCP resource redirect left the configured server origin")
        response = await asyncio.to_thread(_fetch_manual, current, resolved.get("headers") or {})
        if response.status < 300 or response.status >= 400:
            return response
        if redirects >= redirect_limit:
            raise ValueError("MCP resource redirect limit exceeded")
        location = _header(response.headers, "location")
        if not location:
            raise ValueError("MCP resource redirect omitted Location")
        current = urljoin(current, location)
    raise AssertionError("unreachable")


def _fetch_manual(url: str, headers: Mapping[str, str]) -> McpResourceResponse:
    opener = build_opener(_NoRedirect())
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with opener.open(request, timeout=30) as response:
            return McpResourceResponse(response.status, response.read(), dict(response.headers))
    except HTTPError as exc:
        try:
            return McpResourceResponse(exc.code, exc.read(), dict(exc.headers or {}))
        finally:
            exc.close()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


canonical_mcp_origin = canonicalMcpOrigin
fetch_mcp_resource_with_bound_credentials = fetchMcpResourceWithBoundCredentials
