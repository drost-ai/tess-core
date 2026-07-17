from __future__ import annotations

from .clients import JsonRpcClient, _McpHttpUnauthorized
from .http_body import *
from .http_body import _open_without_redirects, _validate_outgoing_headers
from .shared import *
from .sse_decoder import McpSseDecoder
from .transport_policy import *
from .transport_shared import *


class _HttpExchange:
    def __init__(self, owner: "HttpMcpClient") -> None:
        self.owner = owner
        self.lock = threading.Lock()
        self.responses: set[Any] = set()
        self.workers = 0
        self.release_requested = False
        self.finished = False
        self.cancelled = False
        self.cancel_error: BaseException | None = None

    def start_worker(self) -> None:
        with self.lock:
            self.workers += 1

    def finish_worker(self) -> None:
        finish = False
        with self.lock:
            self.workers -= 1
            finish = self.release_requested and self.workers == 0 and not self.finished
            if finish:
                self.finished = True
        if finish:
            self.owner._finish_exchange(self)

    def bind(self, response: Any) -> None:
        with self.lock:
            if self.cancelled:
                _close_http_response(response)
                raise self.cancel_error or McpTransportError(
                    "transport_closed",
                    "MCP HTTP exchange was cancelled.",
                )
            self.responses.add(response)

    def unbind(self, response: Any) -> None:
        with self.lock:
            self.responses.discard(response)

    def cancel(self, error: BaseException | None = None) -> None:
        with self.lock:
            self.cancelled = True
            self.cancel_error = error or self.cancel_error or McpTransportError(
                "transport_closed",
                "MCP HTTP exchange was cancelled.",
            )
            responses = list(self.responses)
        for response in responses:
            _close_http_response(response)

    def release(self) -> None:
        finish = False
        with self.lock:
            self.release_requested = True
            finish = self.workers == 0 and not self.finished
            if finish:
                self.finished = True
        if finish:
            self.owner._finish_exchange(self)


class HttpMcpClient(JsonRpcClient):
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
            if status < 200 or status >= 300:
                _close_http_response(response)
                raise RuntimeError(
                    f"HTTP {status} from MCP server {self.config.get('name')}"
                )
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
        opener = _open_without_redirects
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
            for chunk in iterateMcpBody(response, self.policy):
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
        if request_id is not None and message.get("id") != request_id:
            raise McpTransportError(
                "invalid_framing",
                "MCP HTTP response id does not match its request.",
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
        if isinstance(message.get("method"), str) and message.get("id") is None:
            self.notifications.push(
                {"method": message["method"], "params": message.get("params")}
            )
            return None
        return message if message.get("id") == request_id else None

    async def _start_optional_notification_stream(self) -> None:
        if not self.on_notification:
            return
        resource_url = self.config["commandOrUrl"]
        auth_headers = await self.auth.authorize_headers(resource_url) if self.auth else {}
        thread = threading.Thread(
            target=self._notification_stream_loop,
            args=(resource_url, auth_headers, False, None, None),
            daemon=True,
        )
        self.stream_threads.add(thread)
        thread.start()

    def _notification_stream_loop(
        self,
        url: str,
        auth_headers: dict[str, str],
        require_endpoint: bool,
        ready: threading.Event | None,
        error_ref: dict[str, BaseException] | None,
    ) -> None:
        exchange: _HttpExchange | None = None
        try:
            exchange = self._admit_exchange("notification stream")
            headers = {
                **self._http_headers({"accept": "text/event-stream"}),
                **auth_headers,
            }
            response = self._open_response(url, "GET", headers, None, exchange)
            try:
                if _content_type(response) != "text/event-stream":
                    raise McpTransportError(
                        "invalid_content_type",
                        "Expected text/event-stream from MCP server.",
                    )
                decoder = McpSseDecoder()
                endpoint_seen = False
                for chunk in iterateMcpBody(response, self.policy, continuous=True):
                    if self.stream_stop.is_set():
                        break
                    for event in decoder.push(chunk):
                        endpoint_seen = self._consume_stream_event(event, url, ready) or endpoint_seen
                    self.notifications.flush()
                for event in decoder.push(b"", final=True):
                    endpoint_seen = self._consume_stream_event(event, url, ready) or endpoint_seen
                self.notifications.flush()
                if require_endpoint and not endpoint_seen:
                    raise McpTransportError(
                        "invalid_framing",
                        "MCP SSE stream ended before publishing its endpoint.",
                    )
            finally:
                exchange.unbind(response)
        except BaseException as error:
            if error_ref is not None:
                error_ref["error"] = error
        finally:
            if exchange:
                exchange.release()
            if ready:
                ready.set()
            self.stream_threads.discard(threading.current_thread())

    def _consume_stream_event(
        self,
        event: dict[str, str],
        base_url: str,
        ready: threading.Event | None,
    ) -> bool:
        data = event.get("data")
        if not data:
            return False
        if event.get("event") == "endpoint":
            self._set_endpoint(data, base_url)
            if ready:
                ready.set()
            return True
        message = _decode_json_rpc_payload(data, "SSE event")
        if isinstance(message.get("method"), str) and message.get("id") is None:
            self.notifications.push(
                {"method": message["method"], "params": message.get("params")}
            )
        return False

    def _set_endpoint(self, data: str, base_url: str) -> None:
        return None

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
