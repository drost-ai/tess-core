from __future__ import annotations

import json
import threading
import time
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
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> "FakeProviderHttpServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:
                outer._handle_post(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
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
        self.request_recorder.append(
            {
                "provider": self.provider,
                "api": api,
                "attempt": attempt,
                "method": "POST",
                "path": handler.path,
                "headers": selected_headers(dict(handler.headers.items())),
                "body": json_safe(parsed_body if parsed_body is not None else raw_body),
            }
        )
        response = _select_attempt(self.script, api, attempt)
        delay_ms = int(response.get("delayMs") or 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
        status = int(response.get("status") or 200)
        content_type = str(response.get("contentType") or "application/json")
        body = _response_body(response)
        handler.send_response(status)
        handler.send_header("content-type", content_type)
        handler.send_header("content-length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


def _normalize_script(script: dict[str, Any] | None) -> dict[str, Any]:
    script = script or {}
    if isinstance(script.get("attempts"), list):
        return {"messages": script["attempts"], "count_tokens": script["attempts"]}
    return {
        "messages": script.get("messages") or [_default_messages_response()],
        "count_tokens": script.get("countTokens") or script.get("count_tokens") or [_default_count_tokens_response()],
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


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None


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
