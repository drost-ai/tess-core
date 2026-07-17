from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .._utils import maybe_await
from .auth_loopback import LoopbackListener
from .token_store import mcpTokenStoreKey, token_store_get, token_store_set
from .transport_policy import resolveMcpTransportPolicy


class McpAuthorizationRequiredError(RuntimeError):
    def __init__(self, server: str, authorization_url: str | None = None, continuation_id: str | None = None):
        super().__init__(f"MCP server requires authorization: {server}")
        self.server = server
        self.authorization_url = authorization_url
        self.continuation_id = continuation_id


@dataclass(frozen=True)
class _Pkce:
    verifier: str
    challenge: str


class McpOAuthRuntime:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        store: Any,
        on_runtime_event: Callable[[str, dict[str, Any]], Any] | None = None,
        authorization_callback: Callable[..., Any] | None = None,
    ):
        self.config = config
        self.oauth = config.get("oauth") if isinstance(config.get("oauth"), dict) else {}
        self.store = store
        self.on_runtime_event = on_runtime_event
        self.authorization_callback = authorization_callback
        self.key = mcpTokenStoreKey(config)
        self.resource_origin = canonical_origin(str(config.get("commandOrUrl") or ""))
        self.timeout = resolveMcpTransportPolicy(config)["requestTimeoutMs"] / 1000
        self._refresh_task: asyncio.Task[dict[str, Any]] | None = None
        self._last_authorization_url: str | None = None
        self._last_continuation_id: str | None = None
        self._active_listener: LoopbackListener | None = None
        self._closed = False

    async def authorize_headers(self, resource_url: str) -> dict[str, str]:
        self._ensure_open()
        if canonical_origin(resource_url) != self.resource_origin:
            return {}
        record = await token_store_get(self.store, self.key)
        if not record or not isinstance(record.get("accessToken"), str) or not record.get("accessToken"):
            return {}
        if record.get("resource") and canonical_origin(str(record["resource"])) != self.resource_origin:
            return {}
        if _is_expiring(record) and isinstance(record.get("refreshToken"), str):
            try:
                record = await self._refresh_single_flight(record)
            except Exception:
                self._ensure_open()
        return _bearer(record)

    async def handle_unauthorized(
        self,
        *,
        www_authenticate: str | None,
        resource_url: str,
        sent_authorization: str | None,
    ) -> bool:
        self._ensure_open()
        if canonical_origin(resource_url) != self.resource_origin:
            return False
        record = await token_store_get(self.store, self.key)
        if record and record.get("accessToken") and sent_authorization:
            if _bearer(record).get("Authorization") != sent_authorization:
                return True
        if record and isinstance(record.get("refreshToken"), str) and record.get("refreshToken"):
            try:
                await self._refresh_single_flight(record)
                return True
            except Exception:
                self._ensure_open()
        advertised = bool(_parse_resource_metadata_url(www_authenticate)) or bool(self.oauth)
        if not advertised:
            return False
        await self._authorize(www_authenticate, resource_url)
        return True

    async def has_usable_token(self) -> bool:
        record = await token_store_get(self.store, self.key)
        return bool(record and isinstance(record.get("accessToken"), str) and record.get("accessToken"))

    def authorization_url(self) -> str | None:
        return self._last_authorization_url

    def continuation_id(self) -> str | None:
        return self._last_continuation_id

    async def close(self) -> None:
        self._closed = True
        listener = self._active_listener
        if listener:
            await listener.close()
        task = self._refresh_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _refresh_single_flight(self, record: dict[str, Any]) -> dict[str, Any]:
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh(record))
        return await asyncio.shield(self._refresh_task)

    async def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        metadata = await self._discover_authorization_server(None, str(self.config["commandOrUrl"]))
        fields = {
            "grant_type": "refresh_token",
            "refresh_token": str(record.get("refreshToken") or ""),
            **self._client_auth_fields(record),
        }
        scopes = _scopes(self.oauth)
        if scopes:
            fields["scope"] = " ".join(scopes)
        response = await _request_tokens(metadata["token_endpoint"], fields, self.timeout)
        merged = _merge_token_response(
            record,
            response,
            issuer=metadata.get("issuer"),
            resource=self.resource_origin,
        )
        # Persist a rotated refresh token before any shared waiter resumes.
        await token_store_set(self.store, self.key, merged)
        return merged

    async def _authorize(self, www_authenticate: str | None, resource_url: str) -> None:
        metadata = await self._discover_authorization_server(www_authenticate, resource_url)
        self._ensure_open()
        redirect = self.oauth.get("redirect") if isinstance(self.oauth.get("redirect"), dict) else None
        if not redirect and not callable(self.authorization_callback):
            self._authorization_required(str(metadata["authorization_endpoint"]), uuid.uuid4().hex)
            raise McpAuthorizationRequiredError(
                str(self.config.get("name")), self._last_authorization_url, self._last_continuation_id
            )

        listener: LoopbackListener | None = None
        if redirect:
            listener = await LoopbackListener.start(int(redirect.get("port") or 0))
            if self._closed:
                await listener.close()
                self._ensure_open()
            self._active_listener = listener
            redirect_uri = f"http://127.0.0.1:{listener.port}/callback"
        else:
            redirect_uri = str(self.oauth.get("redirectUri") or "http://127.0.0.1/callback")
        try:
            client_record = await self._ensure_client(metadata, redirect_uri)
            self._ensure_open()
            client_id = str(self.oauth.get("clientId") or client_record.get("clientId") or "")
            pkce = _create_pkce()
            state = _base64_url(secrets.token_bytes(16))
            continuation_id = uuid.uuid4().hex
            authorization_url = _build_authorization_url(
                str(metadata["authorization_endpoint"]),
                client_id=client_id,
                redirect_uri=redirect_uri,
                pkce=pkce,
                scopes=_scopes(self.oauth),
                state=state,
            )
            self._authorization_required(authorization_url, continuation_id)
            if listener:
                timeout = float(redirect.get("timeoutSeconds") or 300)
                code = await listener.wait_for_code(state, timeout)
            else:
                try:
                    callback_result = await maybe_await(
                        self.authorization_callback(
                            {
                                "server": str(self.config.get("name")),
                                "authorizationUrl": authorization_url,
                                "continuationId": continuation_id,
                                "redirectUri": redirect_uri,
                            }
                        )
                    )
                except Exception:
                    raise RuntimeError("MCP OAuth authorization callback failed.") from None
                code = callback_result.get("code") if isinstance(callback_result, dict) else callback_result
                if not isinstance(code, str) or not code:
                    raise RuntimeError("MCP OAuth authorization callback did not return a code.")
            self._ensure_open()
            response = await _request_tokens(
                str(metadata["token_endpoint"]),
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": pkce.verifier,
                    **self._client_auth_fields(client_record),
                    "client_id": client_id,
                },
                self.timeout,
            )
            self._ensure_open()
            merged = _merge_token_response(
                client_record,
                response,
                issuer=metadata.get("issuer"),
                resource=self.resource_origin,
            )
            await token_store_set(self.store, self.key, merged)
            self._last_authorization_url = None
            self._last_continuation_id = None
        finally:
            if listener:
                await listener.close()
                if self._active_listener is listener:
                    self._active_listener = None

    async def _ensure_client(self, metadata: dict[str, Any], redirect_uri: str) -> dict[str, Any]:
        existing = await token_store_get(self.store, self.key) or {"accessToken": ""}
        if self.oauth.get("clientId") or existing.get("clientId"):
            return existing
        endpoint = metadata.get("registration_endpoint")
        if not isinstance(endpoint, str):
            raise RuntimeError(
                f"MCP server {self.config.get('name')} requires OAuth but no client_id is configured."
            )
        body = {
            "client_name": "tess",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        scopes = _scopes(self.oauth)
        if scopes:
            body["scope"] = " ".join(scopes)
        registration = await _fetch_json(
            endpoint,
            method="POST",
            headers={"content-type": "application/json", "accept": "application/json"},
            body=json.dumps(body).encode("utf-8"),
            timeout=self.timeout,
        )
        client_id = registration.get("client_id") if isinstance(registration, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise RuntimeError("Dynamic MCP OAuth client registration did not return a client_id.")
        registered = {
            **existing,
            "clientId": client_id,
            **(
                {"clientSecret": registration["client_secret"]}
                if isinstance(registration.get("client_secret"), str)
                else {}
            ),
        }
        await token_store_set(self.store, self.key, registered)
        return registered

    async def _discover_authorization_server(
        self, www_authenticate: str | None, resource_url: str
    ) -> dict[str, Any]:
        metadata_url = _parse_resource_metadata_url(www_authenticate) or _well_known(
            resource_url, "oauth-protected-resource"
        )
        protected = await _fetch_json_optional(metadata_url, timeout=self.timeout)
        servers = protected.get("authorization_servers") if isinstance(protected, dict) else None
        issuer = servers[0] if isinstance(servers, list) and servers and isinstance(servers[0], str) else canonical_origin(resource_url)
        for suffix in ("oauth-authorization-server", "openid-configuration"):
            metadata = await _fetch_json_optional(_well_known(issuer, suffix), timeout=self.timeout)
            if (
                isinstance(metadata, dict)
                and isinstance(metadata.get("authorization_endpoint"), str)
                and isinstance(metadata.get("token_endpoint"), str)
            ):
                return metadata
        raise RuntimeError(f"Could not discover OAuth metadata for MCP server {self.config.get('name')}.")

    def _client_auth_fields(self, record: dict[str, Any]) -> dict[str, str]:
        client_id = self.oauth.get("clientId") or record.get("clientId")
        client_secret = self.oauth.get("clientSecret") or record.get("clientSecret")
        fields = {}
        if isinstance(client_id, str) and client_id:
            fields["client_id"] = client_id
        if isinstance(client_secret, str) and client_secret:
            fields["client_secret"] = client_secret
        return fields

    def _authorization_required(self, url: str, continuation_id: str) -> None:
        bounded_url = url.encode("utf-8")[:4096].decode("utf-8", errors="ignore")
        self._last_authorization_url = _strip_url_query(bounded_url)
        self._last_continuation_id = continuation_id[:128]
        if callable(self.on_runtime_event):
            self.on_runtime_event(
                "mcp.authorization_required",
                {
                    "server": str(self.config.get("name"))[:256],
                    "transport": str(self.config.get("transport"))[:32],
                    "authorization_url": bounded_url,
                    "continuation_id": self._last_continuation_id,
                },
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"MCP OAuth runtime is closed: {self.config.get('name')}")


def createMcpOAuthRuntime(
    *,
    config: dict[str, Any],
    store: Any,
    on_runtime_event: Callable[[str, dict[str, Any]], Any] | None = None,
    authorization_callback: Callable[..., Any] | None = None,
) -> McpOAuthRuntime:
    return McpOAuthRuntime(
        config=config,
        store=store,
        on_runtime_event=on_runtime_event,
        authorization_callback=authorization_callback,
    )


create_mcp_oauth_runtime = createMcpOAuthRuntime


def canonical_origin(raw: str) -> str:
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    return f"{scheme}://{hostname}{f':{port}' if port else ''}"


def _bearer(record: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"{record.get('tokenType') or 'Bearer'} {record.get('accessToken') or ''}"}


def _is_expiring(record: dict[str, Any]) -> bool:
    expires_at = record.get("expiresAt")
    return isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and expires_at - time.time() * 1000 < 30_000


def _merge_token_response(
    previous: dict[str, Any], response: dict[str, Any], *, issuer: Any, resource: str
) -> dict[str, Any]:
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("MCP OAuth token endpoint did not return an access token.")
    expires_in = response.get("expires_in")
    return {
        **previous,
        "accessToken": access_token,
        "tokenType": response.get("token_type") if isinstance(response.get("token_type"), str) else previous.get("tokenType", "Bearer"),
        **(
            {"refreshToken": response["refresh_token"]}
            if isinstance(response.get("refresh_token"), str)
            else ({"refreshToken": previous["refreshToken"]} if isinstance(previous.get("refreshToken"), str) else {})
        ),
        **(
            {"expiresAt": time.time() * 1000 + float(expires_in) * 1000}
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
            else ({"expiresAt": previous["expiresAt"]} if "expiresAt" in previous else {})
        ),
        **({"scope": response["scope"]} if isinstance(response.get("scope"), str) else {}),
        **({"issuer": issuer} if isinstance(issuer, str) else {}),
        "resource": resource,
    }


async def _request_tokens(endpoint: str, fields: dict[str, str], timeout: float) -> dict[str, Any]:
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


async def _fetch_json_optional(url: str, *, timeout: float) -> dict[str, Any] | None:
    try:
        value = await _fetch_json(url, timeout=timeout)
        return value if isinstance(value, dict) else None
    except _OAuthNotFound:
        return None


async def _fetch_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float,
) -> Any:
    def fetch() -> Any:
        request = Request(url, data=body, headers=headers or {"accept": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(1_048_577)
        except HTTPError as exc:
            if exc.code == 404:
                exc.close()
                raise _OAuthNotFound() from None
            exc.close()
            raise RuntimeError(f"MCP OAuth request failed with HTTP {exc.code}.") from None
        if len(raw) > 1_048_576:
            raise RuntimeError("MCP OAuth response exceeded the allowed size.")
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise RuntimeError("MCP OAuth endpoint returned invalid JSON.") from None

    return await asyncio.to_thread(fetch)


class _OAuthNotFound(Exception):
    pass


def _parse_resource_metadata_url(value: str | None) -> str | None:
    if not value:
        return None
    import re

    match = re.search(r'resource_metadata="?([^",\s]+)"?', value, re.I)
    return match.group(1) if match else None


def _well_known(base: str, suffix: str) -> str:
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    suffix_path = f"/.well-known/{suffix}{path if path and path != '/' else ''}"
    return urlunsplit((parsed.scheme, parsed.netloc, suffix_path, "", ""))


def _strip_url_query(raw: str) -> str:
    parsed = urlsplit(raw)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _create_pkce() -> _Pkce:
    verifier = _base64_url(secrets.token_bytes(32))
    challenge = _base64_url(hashlib.sha256(verifier.encode("ascii")).digest())
    return _Pkce(verifier=verifier, challenge=challenge)


def _base64_url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _build_authorization_url(
    endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    pkce: _Pkce,
    scopes: list[str],
    state: str,
) -> str:
    parsed = urlsplit(endpoint)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.update(
        {
            "response_type": ["code"],
            "client_id": [client_id],
            "redirect_uri": [redirect_uri],
            "code_challenge": [pkce.challenge],
            "code_challenge_method": ["S256"],
            "state": [state],
        }
    )
    if scopes:
        query["scope"] = [" ".join(scopes)]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))


def _scopes(oauth: dict[str, Any]) -> list[str]:
    value = oauth.get("scopes")
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []
