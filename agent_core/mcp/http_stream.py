"""Server-push plumbing for HTTP MCP clients: the GET notification stream,
replies to server-initiated requests, and session termination."""
from __future__ import annotations

import json
import threading
from typing import Any

from .http_body import _close_http_response, _content_type, iterateMcpBody
from .http_exchange import _HttpExchange
from .sse_decoder import McpSseDecoder
from .transport_policy import McpTransportError
from .transport_shared import _decode_json_rpc_payload, _server_request_reply


class _HttpServerPushMixin:
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
            self._handle_stream_error(error)
            if error_ref is not None:
                error_ref["error"] = error
        finally:
            if exchange:
                exchange.release()
            if ready:
                ready.set()
            self.stream_threads.discard(threading.current_thread())

    def _handle_stream_error(self, error: BaseException) -> None:
        return None

    def _consume_stream_event(self, event: dict[str, str], base_url: str, ready: threading.Event | None) -> bool:
        data = event.get("data")
        if not data:
            return False
        if event.get("event") == "endpoint":
            self._set_endpoint(data, base_url)
            if ready:
                ready.set()
            return True
        message = _decode_json_rpc_payload(data, "SSE event")
        if isinstance(message.get("method"), str):
            if message.get("id") is None:
                self.notifications.push(
                    {"method": message["method"], "params": message.get("params")}
                )
            else:
                self._answer_server_request(message["id"], message["method"])
        return False

    def _set_endpoint(self, data: str, base_url: str) -> None:
        return None

    def _answer_server_request(self, request_id: Any, method: str) -> None:
        threading.Thread(
            target=self._post_reply_sync,
            args=(_server_request_reply(request_id, method), dict(self.reply_auth_headers)),
            daemon=True,
        ).start()

    def _post_reply_sync(self, reply: dict[str, Any], auth_headers: dict[str, str]) -> None:
        try:
            exchange = self._admit_exchange("server-request reply")
        except Exception:
            return
        try:
            body = json.dumps(
                reply,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            headers = {
                **self._http_headers(
                    {
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                    }
                ),
                **auth_headers,
            }
            response = self._open_response(self._request_url(), "POST", headers, body, exchange)
            exchange.unbind(response)
            _close_http_response(response)
        except Exception:
            return
        finally:
            exchange.release()

    def _delete_session_sync(self, auth_headers: dict[str, str]) -> None:
        exchange = _HttpExchange(self)
        try:
            headers = {
                **self._http_headers({"accept": "application/json"}),
                **auth_headers,
            }
            response = self._open_response(self._request_url(), "DELETE", headers, None, exchange)
            exchange.unbind(response)
            _close_http_response(response)
        finally:
            exchange.release()
