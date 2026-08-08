from __future__ import annotations

from .clients import JsonRpcClient, _McpHttpUnauthorized
from .http_body import _open_mcp_http, _validate_outgoing_headers
from .http_exchange import _HttpExchange
from .http_stream import _HttpServerPushMixin
import asyncio
import contextlib
import json
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import Request

from .http_body import (
    _close_http_response,
    _content_type,
    _first_header,
    _is_json_content_type,
    iterateMcpBody,
    readMcpBodyText,
    validateMcpResponseHeaders,
)
from .transport_policy import (
    MCP_FRAME_MAX_BYTES,
    MCP_PENDING_REQUEST_MAX,
    McpTransportError,
    resolveMcpTransportPolicy,
)
from .transport_shared import BoundedNotificationQueue, _decode_json_rpc_payload
from .sse_decoder import McpSseDecoder


class HttpMcpClient(_HttpServerPushMixin, JsonRpcClient):
    def __init__(
        self,
        config: dict[str, Any],
        cwd: str,
        on_notification: Callable[[dict[str, Any]], Any] | None = None,
        auth: Any = None,
    ) -> None:
        super().__init__(config, cwd, on_notification, auth)
        self.policy = resolveMcpTransportPolicy(config)
        self.notifications = BoundedNotificationQueue(on_notification)
        self.state = "open"
        self.state_lock = threading.Lock()
        self.exchange_condition = threading.Condition()
        self.exchanges: set[_HttpExchange] = set()
        self.stream_threads: set[threading.Thread] = set()
        self.stream_stop = threading.Event()
        self.close_done = threading.Event()
        self.close_error: BaseException | None = None
        self.session_id: str | None = None
        self.reply_auth_headers: dict[str, str] = {}

    async def initialize(self) -> None:
        await super().initialize()
        if self.config.get("transport") == "http":
            await self._start_optional_notification_stream()

    async def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        exchange = self._admit_exchange(str(request.get("method") or "request"))

        async def workflow() -> dict[str, Any]:
            resource_url = self._request_url()
            auth_headers = await self.auth.authorize_headers(resource_url) if self.auth else {}
            try:
                return await self._run_sync(
                    exchange,
                    self._send_sync,
                    request,
                    auth_headers,
                    exchange,
                )
            except _McpHttpUnauthorized as error:
                if not self.auth:
                    raise RuntimeError(
                        f"HTTP 401 from MCP server {self.config.get('name')}"
                    ) from None
                retry = await self.auth.handle_unauthorized(
                    www_authenticate=error.www_authenticate,
                    resource_url=resource_url,
                    sent_authorization=auth_headers.get("Authorization"),
                )
                if not retry:
                    raise RuntimeError(
                        f"HTTP 401 from MCP server {self.config.get('name')}"
                    ) from None
                auth_headers = await self.auth.authorize_headers(resource_url)
                return await self._run_sync(
                    exchange,
                    self._send_sync,
                    request,
                    auth_headers,
                    exchange,
                )

        try:
            return await asyncio.wait_for(
                workflow(),
                timeout=self.policy["requestTimeoutMs"] / 1000,
            )
        except asyncio.TimeoutError as error:
            timeout = McpTransportError(
                "request_timeout",
                f"MCP request {request.get('method')} exceeded {self.policy['requestTimeoutMs']}ms for {self.config.get('name')}.",
            )
            exchange.cancel(timeout)
            raise timeout from error
        except asyncio.CancelledError:
            exchange.cancel(
                McpTransportError(
                    "transport_closed",
                    f"MCP HTTP request was cancelled: {self.config.get('name')}.",
                )
            )
            raise
        finally:
            exchange.release()

    async def close(self) -> None:
        owner = False
        with self.state_lock:
            if self.state == "closed":
                if self.close_error:
                    raise self.close_error
                return
            if self.state == "open":
                self.state = "closing"
                owner = True
        if not owner:
            await asyncio.to_thread(self.close_done.wait)
            if self.close_error:
                raise self.close_error
            return
        try:
            if self.session_id and self.config.get("transport") == "http":
                # Spec: clients SHOULD terminate their session with a DELETE.
                # Best-effort; 405 means the server declines client termination.
                with contextlib.suppress(BaseException):
                    resource_url = self._request_url()
                    auth_headers = (
                        await self.auth.authorize_headers(resource_url) if self.auth else {}
                    )
                    await asyncio.to_thread(self._delete_session_sync, auth_headers)
            self.stream_stop.set()
            self.notifications.close()
            close_error = McpTransportError(
                "transport_closed",
                f"MCP HTTP client is closing: {self.config.get('name')}.",
            )
            with self.exchange_condition:
                exchanges = list(self.exchanges)
            for exchange in exchanges:
                exchange.cancel(close_error)
            await asyncio.to_thread(self._wait_for_exchanges)
            current = threading.current_thread()
            for thread in list(self.stream_threads):
                if thread is not current and thread.is_alive():
                    await asyncio.to_thread(thread.join, 1)
        except BaseException as error:
            self.close_error = error
            raise
        finally:
            with self.state_lock:
                self.state = "closed"
            self.close_done.set()

    def _request_url(self) -> str:
        return self.config["commandOrUrl"]

    def _http_headers(self, base: dict[str, str]) -> dict[str, str]:
        headers = super()._http_headers(base)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _send_sync(
        self,
        request: dict[str, Any],
        auth_headers: dict[str, str] | None,
        exchange: _HttpExchange,
    ) -> dict[str, Any]:
        try:
            body = json.dumps(
                request,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except Exception as error:
            raise McpTransportError(
                "invalid_framing",
                f"MCP HTTP request could not be encoded: {error}",
            ) from error
        if len(body) > MCP_FRAME_MAX_BYTES:
            raise McpTransportError(
                "frame_limit",
                f"MCP request body exceeds {MCP_FRAME_MAX_BYTES} bytes.",
            )
        headers = {
            **self._http_headers(
                {
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                }
            ),
            **(auth_headers or {}),
        }
        self.reply_auth_headers = dict(auth_headers or {})
        response = self._open_response(
            self._request_url(),
            "POST",
            headers,
            body,
            exchange,
        )
        try:
            status = int(getattr(response, "status", 200))
            if status == 401:
                challenge = _first_header(
                    getattr(response, "headers", None),
                    "www-authenticate",
                )
                _close_http_response(response)
                raise _McpHttpUnauthorized(challenge)
            if status == 404 and self.session_id:
                # Spec: 404 with a session id means the session expired; the
                # client must re-initialize. Failing the connection routes the
                # next call through a fresh initialize.
                self.session_id = None
                _close_http_response(response)
                raise McpTransportError(
                    "session_expired",
                    f"MCP session expired (HTTP 404) for {self.config.get('name')}.",
                )
            if status < 200 or status >= 300:
                _close_http_response(response)
                raise RuntimeError(
                    f"HTTP {status} from MCP server {self.config.get('name')}"
                )
            session = _first_header(getattr(response, "headers", None), "mcp-session-id")
            if session:
                self.session_id = session
            return self._parse_rpc_response(response, request.get("id"))
        finally:
            exchange.unbind(response)

    def _open_response(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        exchange: _HttpExchange,
    ) -> Any:
        _validate_outgoing_headers(headers)
        request = Request(url, data=body, headers=headers, method=method)
        opener = _open_mcp_http
        try:
            response = opener(
                request,
                self.policy["connectTimeoutMs"] / 1000,
                self.policy["headersTimeoutMs"] / 1000,
                exchange,
            )
        except HTTPError as error:
            if error.code == 401:
                challenge = error.headers.get("WWW-Authenticate") if error.headers else None
                _close_http_response(error)
                raise _McpHttpUnauthorized(challenge) from None
            if error.code == 404 and self.session_id:
                self.session_id = None
                _close_http_response(error)
                raise McpTransportError(
                    "session_expired",
                    f"MCP session expired (HTTP 404) for {self.config.get('name')}.",
                ) from None
            _close_http_response(error)
            raise RuntimeError(
                f"HTTP {error.code} from MCP server {self.config.get('name')}"
            ) from error
        except McpTransportError:
            raise
        exchange.bind(response)
        validateMcpResponseHeaders(response)
        return response

    def _parse_rpc_response(self, response: Any, request_id: int | None) -> dict[str, Any]:
        status = int(getattr(response, "status", 200))
        if status in (202, 204):
            _close_http_response(response)
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        content_type = _content_type(response)
        if content_type == "text/event-stream":
            decoder = McpSseDecoder()
            matched: dict[str, Any] | None = None
            # continuous: servers may hold the POST stream open (keepalives)
            # after the response event, so read incrementally and return on match.
            for chunk in iterateMcpBody(response, self.policy, continuous=True):
                try:
                    for event in decoder.push(chunk):
                        matched = self._consume_rpc_sse_event(event, request_id) or matched
                        if matched:
                            return matched
                finally:
                    self.notifications.flush()
            try:
                for event in decoder.push(b"", final=True):
                    matched = self._consume_rpc_sse_event(event, request_id) or matched
                    if matched:
                        return matched
            finally:
                self.notifications.flush()
            raise McpTransportError(
                "invalid_framing",
                "No matching SSE JSON-RPC response was received.",
            )
        if not _is_json_content_type(content_type):
            _close_http_response(response)
            raise McpTransportError(
                "invalid_content_type",
                f"Unexpected MCP content type: {content_type or 'missing'}.",
            )
        text = readMcpBodyText(response, self.policy)
        if not text.strip():
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        message = _decode_json_rpc_payload(text, "HTTP body")
        if request_id is not None:
            if "id" not in message:
                raise McpTransportError(
                    "invalid_framing",
                    "MCP HTTP response is missing JSON-RPC id.",
                )
            if message.get("id") != request_id:
                raise McpTransportError(
                    "invalid_framing",
                    "MCP HTTP response id did not match its request.",
                )
        return message

    def _consume_rpc_sse_event(
        self,
        event: dict[str, str],
        request_id: int | None,
    ) -> dict[str, Any] | None:
        data = event.get("data")
        if not data:
            return None
        message = _decode_json_rpc_payload(data, "SSE event")
        if isinstance(message.get("method"), str):
            # Method present: a notification or a server-initiated request —
            # never our response, even when its id collides with request_id.
            if message.get("id") is None:
                self.notifications.push(
                    {"method": message["method"], "params": message.get("params")}
                )
            else:
                self._answer_server_request(message["id"], message["method"])
            return None
        return message if message.get("id") == request_id else None

    async def _run_sync(self, exchange: _HttpExchange, function: Callable[..., Any], *args: Any) -> Any:
        exchange.start_worker()

        def invoke() -> Any:
            try:
                return function(*args)
            except BaseException as error:
                with exchange.lock:
                    cancelled = exchange.cancelled
                    cancel_error = exchange.cancel_error
                if cancelled and cancel_error:
                    raise cancel_error from error
                raise
            finally:
                exchange.finish_worker()

        return await asyncio.to_thread(invoke)

    def _admit_exchange(self, operation: str) -> _HttpExchange:
        exchange = _HttpExchange(self)
        with self.state_lock:
            if self.state != "open":
                raise McpTransportError(
                    "transport_closed",
                    f"Cannot {operation}: MCP HTTP client is {self.state} ({self.config.get('name')}).",
                )
            with self.exchange_condition:
                if len(self.exchanges) >= MCP_PENDING_REQUEST_MAX:
                    raise McpTransportError(
                        "pending_limit",
                        f"MCP HTTP pending request limit {MCP_PENDING_REQUEST_MAX} reached for {self.config.get('name')}.",
                    )
                self.exchanges.add(exchange)
        return exchange

    def _finish_exchange(self, exchange: _HttpExchange) -> None:
        with self.exchange_condition:
            self.exchanges.discard(exchange)
            self.exchange_condition.notify_all()

    def _wait_for_exchanges(self) -> None:
        deadline = time.monotonic() + max(self.policy.values()) / 1000 + 1
        with self.exchange_condition:
            while self.exchanges and time.monotonic() < deadline:
                self.exchange_condition.wait(timeout=min(0.05, deadline - time.monotonic()))
        if self.exchanges:
            raise RuntimeError(
                f"MCP HTTP exchanges did not drain during close: {self.config.get('name')}."
            )

    def _assert_open(self, operation: str) -> None:
        with self.state_lock:
            state = self.state
        if state != "open":
            raise McpTransportError(
                "transport_closed",
                f"Cannot {operation}: MCP HTTP client is {state} ({self.config.get('name')}).",
            )
