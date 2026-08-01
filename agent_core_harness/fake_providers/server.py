from __future__ import annotations

import json
import select
import socket
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .model import json_safe, selected_headers


class FakeProviderHttpServer:
    def __init__(
        self,
        *,
        provider: str,
        script: dict[str, Any] | None,
        request_recorder: list[dict[str, Any]],
    ) -> None:
        self.provider = provider
        self.script = _normalize_script(script)
        self.request_recorder = request_recorder
        self.counts: dict[str, int] = {}
        self._connections: dict[tuple[str, int], int] = {}
        self._connection_lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> FakeProviderHttpServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:
                outer._handle_post(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(
            target=lambda: self.server and self.server.serve_forever(poll_interval=0.01),
            daemon=True,
        )
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}/v1"
        return self

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1)

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        api = _api_from_path(handler.path)
        length = int(handler.headers.get("content-length") or 0)
        raw_body = handler.rfile.read(length).decode("utf-8") if length else ""
        parsed_body = _parse_json(raw_body)
        self.counts[api] = self.counts.get(api, 0) + 1
        attempt = self.counts[api]
        connection_id, first_on_connection = self._connection_identity(handler)
        record = {
                "provider": self.provider,
                "api": api,
                "attempt": attempt,
                "method": "POST",
                "path": handler.path,
                "connectionId": connection_id,
                "headers": selected_headers(dict(handler.headers.items())),
                "body": json_safe(parsed_body if parsed_body is not None else raw_body),
            }
        self.request_recorder.append(record)
        response = _select_attempt(self.script, api, attempt)
        setup_delay_ms = int(self.script.get("connectionSetupDelayMs") or 0)
        if first_on_connection and setup_delay_ms > 0:
            time.sleep(setup_delay_ms / 1000)
        delay_ms = int(response.get("headersDelayMs") or response.get("delayMs") or 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
        if response.get("fault") == "disconnect_before_headers":
            handler.close_connection = True
            with suppress(OSError):
                handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
            return
        status = int(response.get("status") or 200)
        content_type = str(response.get("contentType") or "application/json")
        chunks = _response_chunks(response)
        body = b"".join(chunk[1] for chunk in chunks)
        handler.send_response(status)
        handler.send_header("content-type", content_type)
        handler.send_header("content-length", str(len(body)))
        handler.end_headers()
        body_delay_ms = int(response.get("bodyDelayMs") or 0)
        if body_delay_ms > 0 and _wait_for_body_delay_or_disconnect(
            handler.connection,
            body_delay_ms,
        ):
            record["clientDisconnected"] = True
            return
        for chunk_delay_ms, chunk in chunks:
            if chunk_delay_ms > 0:
                time.sleep(chunk_delay_ms / 1000)
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                record["clientDisconnected"] = True
                break
        if response.get("closeAfterResponse"):
            handler.close_connection = True
            with suppress(OSError):
                handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()

    def _connection_identity(
        self, handler: BaseHTTPRequestHandler
    ) -> tuple[int, bool]:
        key = (str(handler.client_address[0]), int(handler.client_address[1]))
        with self._connection_lock:
            existing = self._connections.get(key)
            if existing is not None:
                return existing, False
            connection_id = len(self._connections) + 1
            self._connections[key] = connection_id
            return connection_id, True

    @property
    def connection_count(self) -> int:
        with self._connection_lock:
            return len(self._connections)


def _normalize_script(script: dict[str, Any] | None) -> dict[str, Any]:
    script = script or {}
    if isinstance(script.get("attempts"), list):
        return {
            "messages": script["attempts"],
            "count_tokens": script["attempts"],
            "connectionSetupDelayMs": script.get("connectionSetupDelayMs", 0),
        }
    return {
        "messages": script.get("messages") or [_default_messages_response()],
        "count_tokens": script.get("countTokens") or script.get("count_tokens") or [_default_count_tokens_response()],
        "connectionSetupDelayMs": script.get("connectionSetupDelayMs", 0),
    }


def _api_from_path(path: str) -> str:
    parsed = urlparse(path)
    if parsed.path.endswith("/messages/count_tokens"):
        return "count_tokens"
    if parsed.path.endswith("/messages"):
        return "messages"
    return parsed.path.rsplit("/", 1)[-1] or "unknown"


def _select_attempt(script: dict[str, Any], api: str, attempt: int) -> dict[str, Any]:
    attempts = script.get(api) or []
    if not attempts:
        return {"status": 404, "body": {"error": "No fake provider route configured."}}
    selected = attempts[min(attempt - 1, len(attempts) - 1)]
    return selected if isinstance(selected, dict) else {"body": selected}


def _response_body(response: dict[str, Any]) -> bytes:
    if "rawBody" in response:
        return str(response["rawBody"]).encode("utf-8")
    body = response.get("body", {})
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body).encode("utf-8")


def _response_chunks(response: dict[str, Any]) -> list[tuple[int, bytes]]:
    raw_chunks = response.get("chunks")
    if not isinstance(raw_chunks, list):
        return [(0, _response_body(response))]
    chunks: list[tuple[int, bytes]] = []
    for item in raw_chunks:
        if isinstance(item, dict):
            delay_ms = int(item.get("delayMs") or 0)
            raw = item.get("raw", item.get("body", ""))
        else:
            delay_ms = 0
            raw = item
        chunks.append((delay_ms, str(raw).encode("utf-8")))
    return chunks


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _wait_for_body_delay_or_disconnect(connection: socket.socket, delay_ms: int) -> bool:
    deadline = time.monotonic() + (delay_ms / 1000)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        readable, _, _ = select.select([connection], [], [], min(0.01, remaining))
        if not readable:
            continue
        try:
            return connection.recv(1, socket.MSG_PEEK) == b""
        except (BlockingIOError, OSError):
            return True


def _default_messages_response() -> dict[str, Any]:
    return {
        "body": {
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
    }


def _default_count_tokens_response() -> dict[str, Any]:
    return {"body": {"input_tokens": 1}}
