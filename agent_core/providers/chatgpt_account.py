"""Sign in with ChatGPT for an open-source agent, as OpenAI documents it
(developers.openai.com/siwc/token-sharing-open-source).

One attempt is a loopback listener on 127.0.0.1 at the fixed callback path,
an authorization URL for the owner's own browser, and a code exchange. The
first sign-in registers this agent (``client_id=dynamic_agent_client`` with
the app's real name) and the callback hands back an issued client id, which
is kept with the account and used for every later sign-in, refresh and
revocation. Every attempt has a fresh state, nonce and PKCE S256 verifier.
The ID token is verified against OpenAI's published keys (RS256: issuer,
audience = the issued client id, expiry, nonce); ``sub`` is the account.
Signing in proves identity only: plan use needs the granted scope
``chatgpt.tokens.use.direct``, and without it the account is kept with plan
use off.

Tokens never appear in errors, logs or ``repr``; the authorization URL can
carry an ID-token hint and is only ever handed to the browser. HTTP goes
through ``opener`` (urllib in a worker thread by default) so tests answer
with a fake server. ID-token signatures need the ``cryptography`` package
(the ``chatgpt-account`` extra); without it sign-in fails closed.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

INITIAL_CLIENT = "dynamic_agent_client"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
RESOURCE = "https://api.openai.com/v1"
CALLBACK_PATH = "/auth/callback"
PREFERRED_PORT = 1455
MAX_RESPONSE_BYTES = 1024 * 1024
LEEWAY_SECONDS = 60
# Refresh errors after which only signing in again helps (errors-and-recovery).
TERMINAL_REFRESH_ERRORS = frozenset({"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                                      "refresh_token_invalidated", "refresh_token_reused"})


@dataclass(frozen=True)
class Endpoints:
    authorize: str = "https://auth.openai.com/api/accounts/authorize"
    token: str = "https://auth.openai.com/api/accounts/oauth/token"
    revoke: str = "https://auth.openai.com/api/accounts/oauth/revoke"
    jwks: str = "https://auth.openai.com/.well-known/jwks.json"
    issuer: str = "https://auth.openai.com"


OPENAI = Endpoints()


class ChatGPTAccountError(Exception):
    """A sign-in, refresh or revocation that did not succeed; ``code`` is
    static and safe to show, never a token or provider text."""

    def __init__(self, code: str, *, terminal: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.terminal = terminal


# (method, url, form fields or None) -> (status, body bytes)
Opener = Callable[[str, str, "dict[str, str] | None"], Awaitable["tuple[int, bytes]"]]


async def urllib_opener(method: str, url: str, form: dict[str, str] | None) -> tuple[int, bytes]:
    def call() -> tuple[int, bytes]:
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/json", **({"Content-Type": "application/x-www-form-urlencoded"} if data else {})})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 (fixed https endpoints)
                return response.status, response.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]
        except urllib.error.HTTPError as error:
            return error.code, error.read(MAX_RESPONSE_BYTES)
    return await asyncio.to_thread(call)


@dataclass(frozen=True)
class Grant:
    """A verified sign-in: the account, the issued client and its tokens."""

    client_id: str
    account_id: str  # the ID token's sub
    email: str
    scope: tuple[str, ...]
    plan_use: bool
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    id_token: str = field(repr=False)
    expires_at: float
    earliest_refresh_at: float | None = None

    def bundle(self) -> dict[str, Any]:
        """The credential-store payload (purpose provider_oauth_token)."""
        return {"access_token": self.access_token, "refresh_token": self.refresh_token, "id_token": self.id_token,
                "expires_at": self.expires_at, "client_id": self.client_id, "account_id": self.account_id,
                **({"earliest_refresh_at": self.earliest_refresh_at} if self.earliest_refresh_at is not None else {})}


def new_host_id() -> str:
    """A stable per-host ``ext_agent_host_id``: made once, kept by the caller."""
    return secrets.token_urlsafe(24)


class SignIn:
    """One sign-in attempt. ``begin`` (or ``prepare`` without a socket), hand
    ``url`` to the browser, then ``finish``."""

    def __init__(self, *, host_id: str, app_name: str, client_id: str | None = None, account_id: str | None = None,
                 id_token_hint: str | None = None, login_hint: str | None = None, port: int = PREFERRED_PORT,
                 endpoints: Endpoints = OPENAI, opener: Opener | None = None, clock: Callable[[], float] = time.time) -> None:
        if not host_id or not app_name:
            raise ChatGPTAccountError("invalid_request")
        self.host_id, self.app_name = host_id, app_name
        self.saved_client, self.saved_account = client_id, account_id
        self.id_token_hint, self.login_hint = id_token_hint, login_hint
        self.port, self.endpoints = port, endpoints
        self.opener = opener or urllib_opener
        self.clock = clock
        self.state = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        self._callback: asyncio.Future[dict[str, str]] | None = None
        self._server: asyncio.base_events.Server | None = None

    # -- the browser leg ----------------------------------------------------

    @classmethod
    async def begin(cls, **kwargs: Any) -> SignIn:
        """Listen on 127.0.0.1 (the preferred port, else any) before the URL exists."""
        attempt = cls(**kwargs)
        try:
            attempt._server = await asyncio.start_server(attempt._serve, "127.0.0.1", attempt.port)
        except OSError:
            attempt._server = await asyncio.start_server(attempt._serve, "127.0.0.1", 0)
        attempt.port = attempt._server.sockets[0].getsockname()[1]
        return attempt

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"  # never localhost

    @property
    def url(self) -> str:
        """For the owner's browser only: a reauthorization carries an ID-token hint."""
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": self.saved_client or INITIAL_CLIENT, "ext_agent_host_id": self.host_id, "response_type": "code",
                  "redirect_uri": self.redirect_uri, "scope": SCOPE, "resource": RESOURCE, "state": self.state,
                  "nonce": self.nonce, "code_challenge_method": "S256", "code_challenge": challenge}
        if self.saved_client:
            params.update({key: value for key, value in (("id_token_hint", self.id_token_hint), ("login_hint", self.login_hint)) if value})
        else:
            params["agent_name_hint"] = self.app_name
        return f"{self.endpoints.authorize}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"

    def deliver(self, target: str) -> tuple[int, str]:
        """One request to the callback listener (path and query); returns the
        status and page for the browser. Only the matching state counts."""
        parsed = urllib.parse.urlsplit(target)
        if parsed.path != CALLBACK_PATH:
            return 404, _page("Not found.")
        query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        if not secrets.compare_digest(query.get("state", ""), self.state):
            return 400, _page("This sign-in link does not belong to the sign-in Tess is waiting for.")
        future = self._future()
        if future.done():
            return 409, _page("This sign-in was already completed. You can close this tab.")
        future.set_result(query)
        if "error" in query:
            return 200, _page("Sign-in was not completed. You can close this tab and return to Tess.")
        return 200, _page("Signed in. You can close this tab and return to Tess.")

    async def finish(self, timeout: float = 300.0) -> Grant:
        try:
            query = await asyncio.wait_for(asyncio.shield(self._future()), timeout)
        except asyncio.TimeoutError:
            raise ChatGPTAccountError("timed_out") from None
        finally:
            await self.close()
        if query.get("error") == "access_denied":
            raise ChatGPTAccountError("access_denied")
        if query.get("error") or not query.get("code"):
            raise ChatGPTAccountError("authorization_failed")
        issued = query.get("client_id") or ""
        if self.saved_client:
            if issued and issued != self.saved_client:
                raise ChatGPTAccountError("client_mismatch")
            client = self.saved_client
        else:
            if not issued or issued == INITIAL_CLIENT:
                raise ChatGPTAccountError("registration_incomplete")
            client = issued
        tokens = await token_request(self.endpoints, self.opener, {
            "grant_type": "authorization_code", "client_id": client, "code": query["code"],
            "code_verifier": self.verifier, "redirect_uri": self.redirect_uri, "resource": RESOURCE}, refresh=False)
        claims = verify_id_token(str(tokens.get("id_token") or ""), jwks=await fetch_jwks(self.endpoints, self.opener),
                                 issuer=self.endpoints.issuer, audience=client, nonce=self.nonce, now=self.clock())
        if self.saved_account and claims["sub"] != self.saved_account:
            raise ChatGPTAccountError("account_mismatch")  # the selected account is kept
        return grant_from(tokens, client_id=client, account_id=claims["sub"], email=str(claims.get("email") or ""), now=self.clock())

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _future(self) -> asyncio.Future[dict[str, str]]:
        if self._callback is None:
            self._callback = asyncio.get_running_loop().create_future()
        return self._callback

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            status, body = self.deliver(target) if method == "GET" else (405, _page("Method not allowed."))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            status, body = 400, _page("Bad request.")
        data = body.encode()
        writer.write(f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\nContent-Type: text/html; charset=utf-8\r\n"
                     f"Content-Length: {len(data)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + data)
        try:
            await writer.drain()
        finally:
            writer.close()


# -- the token endpoint, keys and identity ---------------------------------


async def token_request(endpoints: Endpoints, opener: Opener, form: dict[str, str], *, refresh: bool) -> dict[str, Any]:
    try:
        status, body = await opener("POST", endpoints.token, form)
    except Exception:  # noqa: BLE001 (a network failure: transient, credentials kept)
        raise ChatGPTAccountError("token_endpoint_unreachable") from None
    payload = _json(body)
    if status == 200 and isinstance(payload.get("access_token"), str) and payload.get("access_token"):
        return payload
    error = str(payload.get("error") or "")
    if refresh and error in TERMINAL_REFRESH_ERRORS:
        raise ChatGPTAccountError("reauthorization_required", terminal=True)
    if error == "invalid_client":
        raise ChatGPTAccountError("invalid_client", terminal=True)
    if not refresh and error == "invalid_grant":
        raise ChatGPTAccountError("code_rejected")  # start a new authorization
    raise ChatGPTAccountError("token_endpoint_unavailable" if status >= 500 or status == 429 else "token_request_failed")


def grant_from(tokens: dict[str, Any], *, client_id: str, account_id: str, email: str, now: float,
               previous: dict[str, Any] | None = None) -> Grant:
    """A token response as a Grant; a refresh without a new refresh or ID
    token keeps the previous ones."""
    previous = previous or {}
    scope = tuple(str(tokens.get("scope") or "").split()) or tuple(str(previous.get("scope") or "").split())
    try:
        lifetime = float(tokens.get("expires_in") or 3600)
    except (TypeError, ValueError):
        lifetime = 3600.0
    earliest = tokens.get("earliest_refresh_at")
    refresh_token = tokens.get("refresh_token") or previous.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ChatGPTAccountError("token_response_invalid")
    return Grant(client_id=client_id, account_id=account_id, email=email, scope=scope, plan_use=PLAN_SCOPE in scope,
                 access_token=str(tokens["access_token"]), refresh_token=refresh_token,
                 id_token=str(tokens.get("id_token") or previous.get("id_token") or ""), expires_at=now + lifetime,
                 earliest_refresh_at=float(earliest) if isinstance(earliest, (int, float)) else None)


async def fetch_jwks(endpoints: Endpoints, opener: Opener) -> dict[str, Any]:
    try:
        status, body = await opener("GET", endpoints.jwks, None)
    except Exception:  # noqa: BLE001
        raise ChatGPTAccountError("keys_unreachable") from None
    keys = _json(body)
    if status != 200 or not isinstance(keys.get("keys"), list):
        raise ChatGPTAccountError("keys_unreachable")
    return keys


def verify_id_token(token: str, *, jwks: dict[str, Any], issuer: str, audience: str, nonce: str, now: float) -> dict[str, Any]:
    """The verified claims of an RS256 ID token, or ChatGPTAccountError("id_token_invalid")."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    except ImportError:
        raise ChatGPTAccountError("chatgpt_account_extra_missing") from None
    try:
        head, body, signature = token.split(".")
        header, claims = json.loads(_unb64(head)), json.loads(_unb64(body))
        if header.get("alg") != "RS256":
            raise ValueError()
        candidates = [key for key in jwks.get("keys", []) if key.get("kty") == "RSA" and (not header.get("kid") or key.get("kid") == header.get("kid"))]
        for key in candidates:
            public = RSAPublicNumbers(int.from_bytes(_unb64(key["e"]), "big"), int.from_bytes(_unb64(key["n"]), "big")).public_key()
            try:
                public.verify(_unb64(signature), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
                break
            except InvalidSignature:
                continue
        else:
            raise ValueError()
        audiences = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
        if (claims.get("iss") != issuer or audience not in audiences or not isinstance(claims.get("sub"), str) or not claims["sub"]
                or float(claims.get("exp", 0)) < now - LEEWAY_SECONDS or float(claims.get("iat", now)) > now + LEEWAY_SECONDS
                or not secrets.compare_digest(str(claims.get("nonce") or ""), nonce)):
            raise ValueError()
        return claims
    except ChatGPTAccountError:
        raise
    except Exception:  # noqa: BLE001 (any malformed or unverifiable token)
        raise ChatGPTAccountError("id_token_invalid") from None


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _page(message: str) -> str:
    return f"<!doctype html><meta charset=utf-8><title>Tess</title><p style='font:16px system-ui;margin:3em'>{message}</p>"
