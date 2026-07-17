from __future__ import annotations

from .clients import JsonRpcClient
from .http_body import _close_http_response
from .http_transport import HttpMcpClient
from .shared import *
from .transport_policy import MCP_PENDING_REQUEST_MAX, McpTransportError
from .transport_shared import _decode_json_rpc_payload


class SseMcpClient(HttpMcpClient):
    def __init__(
        self,
        config: dict[str, Any],
        cwd: str,
        on_notification: Callable[[dict[str, Any]], Any] | None = None,
        auth: Any = None,
    ) -> None:
        super().__init__(config, cwd, on_notification, auth)
        self.post_url = config["commandOrUrl"]
        self.endpoint_ready = threading.Event()
        self.stream_error: dict[str, BaseException] = {}
        self.pending: dict[int, queue.Queue[dict[str, Any] | BaseException]] = {}
        self.pending_lock = threading.Lock()

    async def initialize(self) -> None:
        resource_url = self.config["commandOrUrl"]
        auth_headers = await self.auth.authorize_headers(resource_url) if self.auth else {}
        thread = threading.Thread(
            target=self._notification_stream_loop,
            args=(resource_url, auth_headers, True, self.endpoint_ready, self.stream_error),
            daemon=True,
        )
        self.stream_threads.add(thread)
        thread.start()
        await asyncio.to_thread(self.endpoint_ready.wait)
        if self.stream_error.get("error"):
            raise self.stream_error["error"]
        await JsonRpcClient.initialize(self)

    async def close(self) -> None:
        self._reject_pending(
            McpTransportError(
                "transport_closed",
                f"MCP SSE client is closing: {self.config.get('name')}.",
            )
        )
        await super().close()

    async def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        request_id = request.get("id")
        response_queue: queue.Queue[dict[str, Any] | BaseException] | None = None
        if isinstance(request_id, int) and not isinstance(request_id, bool):
            response_queue = queue.Queue(maxsize=1)
            with self.pending_lock:
                if len(self.pending) >= MCP_PENDING_REQUEST_MAX:
                    raise McpTransportError(
                        "pending_limit",
                        f"MCP SSE pending request limit {MCP_PENDING_REQUEST_MAX} reached for {self.config.get('name')}.",
                    )
                self.pending[request_id] = response_queue
        try:
            response = await super()._send(request)
            if not response_queue or not response.pop("_mcp_sse_empty_ack", False):
                return response
            remaining = (
                self.policy["requestTimeoutMs"] / 1000
                - (time.monotonic() - started)
            )
            if remaining <= 0:
                raise McpTransportError(
                    "request_timeout",
                    f"MCP request {request.get('method')} exceeded {self.policy['requestTimeoutMs']}ms for {self.config.get('name')}.",
                )
            try:
                queued = await asyncio.to_thread(
                    response_queue.get,
                    True,
                    remaining,
                )
            except queue.Empty as error:
                raise McpTransportError(
                    "request_timeout",
                    f"MCP request {request.get('method')} exceeded {self.policy['requestTimeoutMs']}ms for {self.config.get('name')}.",
                ) from error
            if isinstance(queued, BaseException):
                raise queued
            return queued
        finally:
            if response_queue:
                with self.pending_lock:
                    if self.pending.get(request_id) is response_queue:
                        self.pending.pop(request_id, None)

    def _parse_rpc_response(
        self,
        response: Any,
        request_id: int | None,
    ) -> dict[str, Any]:
        if int(getattr(response, "status", 200)) in (202, 204):
            _close_http_response(response)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {},
                "_mcp_sse_empty_ack": True,
            }
        return super()._parse_rpc_response(response, request_id)

    def _request_url(self) -> str:
        with self.state_lock:
            return self.post_url

    def _handle_sse_event(self, event_name: str | None, data_parts: list[str]) -> None:
        event = {"data": "\n".join(data_parts)}
        if event_name:
            event["event"] = event_name
        self._consume_stream_event(event, self.config["commandOrUrl"], None)
        self.notifications.flush()

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
        request_id = message.get("id")
        if isinstance(message.get("method"), str) and request_id is None:
            self.notifications.push(
                {"method": message["method"], "params": message.get("params")}
            )
            return False
        if isinstance(request_id, int) and not isinstance(request_id, bool):
            with self.pending_lock:
                response_queue = self.pending.pop(request_id, None)
            if response_queue:
                response_queue.put_nowait(message)
                return False
        if "result" in message or "error" in message:
            self._reject_pending(
                McpTransportError(
                    "invalid_framing",
                    "MCP SSE response id did not match a pending request.",
                )
            )
        return False

    def _reject_pending(self, error: BaseException) -> None:
        with self.pending_lock:
            pending = list(self.pending.values())
            self.pending.clear()
        for response_queue in pending:
            with contextlib.suppress(queue.Full):
                response_queue.put_nowait(error)

    def _handle_stream_error(self, error: BaseException) -> None:
        self._reject_pending(error)

    def _set_endpoint(self, data: str, base_url: str) -> None:
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = None
        candidate = (
            parsed.get("url")
            if isinstance(parsed, dict) and isinstance(parsed.get("url"), str)
            else parsed.get("endpoint")
            if isinstance(parsed, dict) and isinstance(parsed.get("endpoint"), str)
            else data
        )
        endpoint = urljoin(base_url, candidate)
        base = urlparse(base_url)
        target = urlparse(endpoint)
        if target.scheme not in {"http", "https"} or not target.hostname:
            raise McpTransportError(
                "invalid_framing",
                "MCP SSE endpoint is not HTTP(S).",
            )
        if _url_origin(base) != _url_origin(target):
            raise McpTransportError(
                "invalid_framing",
                "MCP SSE endpoint crossed the configured server origin.",
            )
        with self.state_lock:
            self.post_url = endpoint


def _url_origin(value: Any) -> tuple[str, str, int]:
    default = 443 if value.scheme == "https" else 80
    return value.scheme.lower(), str(value.hostname or "").lower(), value.port or default
