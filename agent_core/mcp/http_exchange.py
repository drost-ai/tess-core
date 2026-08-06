"""Refcounted HTTP exchange wrapper so close()/timeouts can force-close
in-flight MCP responses."""
from __future__ import annotations

import threading
from typing import Any

from .http_body import _close_http_response
from .transport_policy import McpTransportError

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
