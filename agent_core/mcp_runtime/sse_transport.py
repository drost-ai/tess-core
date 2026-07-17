from __future__ import annotations

from .clients import JsonRpcClient
from .http_transport import HttpMcpClient
from .shared import *
from .transport_policy import McpTransportError


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

    def _request_url(self) -> str:
        with self.state_lock:
            return self.post_url

    def _handle_sse_event(self, event_name: str | None, data_parts: list[str]) -> None:
        event = {"data": "\n".join(data_parts)}
        if event_name:
            event["event"] = event_name
        self._consume_stream_event(event, self.config["commandOrUrl"], None)
        self.notifications.flush()

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
