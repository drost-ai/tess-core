from __future__ import annotations

import asyncio
import http.server
import queue
import threading
from urllib.parse import parse_qs, urlsplit


class LoopbackListener:
    """OAuth redirect receiver; it never opens or controls a browser."""

    def __init__(
        self,
        server: http.server.ThreadingHTTPServer,
        response_queue: queue.Queue[tuple[str | None, str | None]],
    ):
        self.server = server
        self.response_queue = response_queue
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._closed = False
        self._close_lock = asyncio.Lock()
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @classmethod
    async def start(cls, port: int) -> "LoopbackListener":
        response_queue: queue.Queue[tuple[str | None, str | None]] = queue.Queue(maxsize=1)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                values = parse_qs(urlsplit(self.path).query)
                code = values.get("code", [None])[0]
                state = values.get("state", [None])[0]
                try:
                    response_queue.put_nowait((code, state))
                except queue.Full:
                    pass
                self.send_response(200 if code else 400)
                self.send_header("content-type", "text/plain")
                self.end_headers()
                body = (
                    b"Authorization complete. You may close this window."
                    if code
                    else b"Invalid authorization response."
                )
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = await asyncio.to_thread(
            http.server.ThreadingHTTPServer,
            ("127.0.0.1", port),
            Handler,
        )
        return cls(server, response_queue)

    async def wait_for_code(self, expected_state: str, timeout: float) -> str:
        try:
            code, state = await asyncio.to_thread(self.response_queue.get, True, timeout)
        except queue.Empty:
            raise RuntimeError("Timed out waiting for MCP OAuth authorization redirect.") from None
        if not code or state != expected_state:
            raise RuntimeError("MCP OAuth authorization redirect was invalid.")
        return code

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.response_queue.put_nowait((None, None))
            except queue.Full:
                pass
            await asyncio.to_thread(self.server.shutdown)
            await asyncio.to_thread(self.server.server_close)
            await asyncio.to_thread(self.thread.join, 1)
