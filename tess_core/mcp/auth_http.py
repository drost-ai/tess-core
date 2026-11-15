"""HTTP plumbing for MCP OAuth: hardened metadata/token fetches.

Discovery and token requests refuse redirects and non-HTTPS endpoints
(loopback HTTP is allowed for local servers), and bound response sizes.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _oauth_not_found(Exception):
    pass


class _no_oauth_redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _strip_url_query(raw: str) -> str:
    parsed = urlsplit(raw)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _assert_oauth_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host:
        return
    if parsed.scheme == "http" and host in {"127.0.0.1", "::1", "localhost"}:
        return
    raise RuntimeError(
        f"MCP OAuth endpoints must use HTTPS (or loopback HTTP): {_strip_url_query(url)}"
    )


async def _fetch_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float,
) -> Any:
    def fetch() -> Any:
        _assert_oauth_url(url)
        request = Request(url, data=body, headers=headers or {"accept": "application/json"}, method=method)
        try:
            with build_opener(_no_oauth_redirect()).open(request, timeout=timeout) as response:
                raw = response.read(1_048_577)
        except HTTPError as exc:
            if exc.code == 404:
                exc.close()
                raise _oauth_not_found() from None
            if 300 <= exc.code < 400:
                exc.close()
                raise RuntimeError("MCP OAuth endpoint attempted a redirect, which is not allowed.") from None
            exc.close()
            raise RuntimeError(f"MCP OAuth request failed with HTTP {exc.code}.") from None
        if len(raw) > 1_048_576:
            raise RuntimeError("MCP OAuth response exceeded the allowed size.")
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise RuntimeError("MCP OAuth endpoint returned invalid JSON.") from None

    return await asyncio.to_thread(fetch)


async def _fetch_json_optional(url: str, *, timeout: float) -> dict[str, Any] | None:
    try:
        value = await _fetch_json(url, timeout=timeout)
        return value if isinstance(value, dict) else None
    except _oauth_not_found:
        return None


async def _request_tokens(endpoint: str, fields: dict[str, str], timeout: float) -> dict[str, Any]:
    from urllib.parse import urlencode

    result = await _fetch_json(
        endpoint,
        method="POST",
        headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json"},
        body=urlencode(fields).encode("utf-8"),
        timeout=timeout,
    )
    if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
        raise RuntimeError("MCP OAuth token endpoint did not return an access token.")
    return result


def _parse_resource_metadata_url(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r'resource_metadata="?([^",\s]+)"?', value, re.I)
    return match.group(1) if match else None


def _well_known(base: str, suffix: str) -> str:
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    suffix_path = f"/.well-known/{suffix}{path if path and path != '/' else ''}"
    return urlunsplit((parsed.scheme, parsed.netloc, suffix_path, "", ""))
