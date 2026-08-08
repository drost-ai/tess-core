from __future__ import annotations

from tests.support.agent_core_case import *

import socket

from agent_core.mcp.http_transport import HttpMcpClient
from agent_core.mcp.protocol import McpProtocolError, _unwrap_json_rpc_response
from agent_core.mcp.state import (
    _connect_state,
    _create_state,
    _refresh_resources,
    _refresh_tools,
    _request_with_state_tracking,
    _safe_request,
)
from agent_core.mcp.transport_policy import McpTransportError


def _noop_event(*_args, **_kwargs) -> None:
    return None


class _RawHttpServer:
    """Raw-socket HTTP server so tests control framing exactly like real servers do."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[dict] = []
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.port = self.socket.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self) -> None:
        while not self.closed.is_set():
            try:
                conn, _ = self.socket.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        stream = conn.makefile("rb")
        try:
            while not self.closed.is_set():
                line = stream.readline().decode("utf-8", errors="replace")
                if not line:
                    return
                method, path, _ = line.split()
                headers: dict[str, str] = {}
                while True:
                    raw = stream.readline().decode("utf-8", errors="replace").strip()
                    if not raw:
                        break
                    key, _, value = raw.partition(":")
                    headers[key.strip().lower()] = value.strip()
                body = stream.read(int(headers.get("content-length", 0) or 0))
                request = {"method": method, "path": path, "headers": headers, "body": body}
                self.requests.append(request)
                if not self.handler(conn, request):
                    return
        except Exception:
            return
        finally:
            with contextlib.suppress(Exception):
                stream.close()
            with contextlib.suppress(Exception):
                conn.close()

    def close(self) -> None:
        self.closed.set()
        with contextlib.suppress(Exception):
            self.socket.close()


def _json_response(conn: socket.socket, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
        % len(body)
        + body
    )


def _redirect_response(conn: socket.socket, status: int, location: str) -> None:
    conn.sendall(
        f"HTTP/1.1 {status} Redirect\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()
    )


def _http_config(origin: str, path: str = "/mcp", **overrides) -> dict:
    return {
        "name": "interop",
        "transport": "http",
        "commandOrUrl": f"{origin}{path}",
        "requestTimeoutMs": 5000,
        **overrides,
    }


class McpSseStreamedPostTests(AgentCoreTestCase):
    def test_sse_post_response_returns_before_server_closes_stream(self):
        def handler(conn: socket.socket, request: dict) -> bool:
            message = json.loads(request["body"])
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"streamed": True}}
            )
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\nTransfer-Encoding: chunked\r\n\r\n"
            )
            event = f"event: message\ndata: {payload}\n\n".encode()
            conn.sendall(b"%x\r\n" % len(event) + event + b"\r\n")
            # Hold the stream open with keepalives, as real servers do for
            # long-running calls; the client must not wait for stream EOF.
            for _ in range(100):
                if self_server.closed.wait(0.2):
                    break
                with contextlib.suppress(Exception):
                    keepalive = b": keepalive\n\n"
                    conn.sendall(b"%x\r\n" % len(keepalive) + keepalive + b"\r\n")
            return False

        self_server = _RawHttpServer(handler)
        client = HttpMcpClient(_http_config(self_server.origin), os.getcwd())
        try:
            started = time.monotonic()
            result = run(client.request("tools/list"))
            elapsed = time.monotonic() - started
            self.assertEqual(result, {"streamed": True})
            self.assertLess(elapsed, 2.0)
        finally:
            run(client.close())
            self_server.close()


class McpRedirectTests(AgentCoreTestCase):
    def test_post_follows_same_origin_307_redirect(self):
        def handler(conn: socket.socket, request: dict) -> bool:
            if request["path"] == "/mcp":
                _redirect_response(conn, 307, "/mcp/")
                return True
            message = json.loads(request["body"])
            _json_response(
                conn,
                {"jsonrpc": "2.0", "id": message["id"], "result": {"redirected": True}},
            )
            return True

        server = _RawHttpServer(handler)
        client = HttpMcpClient(_http_config(server.origin), os.getcwd())
        try:
            result = run(client.request("tools/list"))
            self.assertEqual(result, {"redirected": True})
            self.assertEqual([item["path"] for item in server.requests], ["/mcp", "/mcp/"])
            self.assertEqual(server.requests[0]["body"], server.requests[1]["body"])
        finally:
            run(client.close())
            server.close()

    def test_cross_origin_redirect_is_refused(self):
        foreign = _RawHttpServer(lambda conn, request: False)

        def handler(conn: socket.socket, request: dict) -> bool:
            _redirect_response(conn, 307, f"{foreign.origin}/steal")
            return True

        server = _RawHttpServer(handler)
        client = HttpMcpClient(_http_config(server.origin), os.getcwd())
        try:
            with self.assertRaises(McpTransportError) as raised:
                run(client.request("tools/list"))
            self.assertEqual(raised.exception.code, "redirect_refused")
            self.assertEqual(foreign.requests, [])
        finally:
            run(client.close())
            server.close()
            foreign.close()

    def test_method_changing_redirect_on_post_is_refused(self):
        def handler(conn: socket.socket, request: dict) -> bool:
            _redirect_response(conn, 303, "/mcp/")
            return True

        server = _RawHttpServer(handler)
        client = HttpMcpClient(_http_config(server.origin), os.getcwd())
        try:
            with self.assertRaises(McpTransportError) as raised:
                run(client.request("tools/list"))
            self.assertEqual(raised.exception.code, "redirect_refused")
        finally:
            run(client.close())
            server.close()

    def test_redirect_loop_stops_at_hop_limit(self):
        def handler(conn: socket.socket, request: dict) -> bool:
            _redirect_response(conn, 308, "/mcp")
            return True

        server = _RawHttpServer(handler)
        client = HttpMcpClient(_http_config(server.origin), os.getcwd())
        try:
            with self.assertRaises(McpTransportError) as raised:
                run(client.request("tools/list"))
            self.assertEqual(raised.exception.code, "redirect_limit")
        finally:
            run(client.close())
            server.close()


def _write_error_fixture(workspace: Path) -> Path:
    target = workspace / "mcp_protocol_errors.py"
    target.write_text(
        textwrap.dedent(
            r'''
            import json
            import os
            import sys

            def send(message):
                sys.stdout.buffer.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
                sys.stdout.buffer.flush()

            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                request = json.loads(line.decode("utf-8"))
                request_id = request.get("id")
                method = request.get("method")
                if method == "initialize":
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "errors", "version": "1"},
                        },
                    })
                elif method == "tools/list":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
                        {"name": "pid", "inputSchema": {"type": "object"}},
                        {"name": "boom", "inputSchema": {"type": "object"}},
                        {"name": "slow", "inputSchema": {"type": "object"}},
                    ]}})
                elif method == "tools/call":
                    name = (request.get("params") or {}).get("name")
                    if name == "boom":
                        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Invalid params"}})
                    elif name == "slow":
                        continue
                    else:
                        send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(os.getpid())}]}})
                elif request_id is not None:
                    send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            '''
        ),
        encoding="utf-8",
    )
    return target


def _stdio_error_config(server: Path, **overrides) -> dict:
    return {
        "name": "errors",
        "transport": "stdio",
        "commandOrUrl": sys.executable,
        "args": [str(server)],
        "startupTimeoutMs": 5000,
        "requestTimeoutMs": 400,
        **overrides,
    }


class McpProtocolErrorSplitTests(AgentCoreTestCase):
    def test_unwrap_raises_typed_protocol_error(self):
        with self.assertRaises(McpProtocolError) as raised:
            _unwrap_json_rpc_response(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid params"}}
            )
        self.assertEqual(raised.exception.code, -32602)
        self.assertEqual(str(raised.exception), "-32602: Invalid params")

    def test_protocol_error_and_timeout_do_not_respawn_stdio_server(self):
        root, home, workspace, previous = self.temp_context()
        try:
            fixture = _write_error_fixture(workspace)
            state = _create_state(_stdio_error_config(fixture))

            async def scenario():
                await _connect_state(state, str(workspace), _noop_event)
                assert state["connected"], state.get("error")

                async def call(name):
                    return await _request_with_state_tracking(
                        state, "tools/call", {"name": name, "arguments": {}}
                    )

                first = await call("pid")
                pid = first["content"][0]["text"]

                # A JSON-RPC error response must not mark the connection failed.
                try:
                    await call("boom")
                except McpProtocolError as error:
                    protocol_error = error
                else:
                    raise AssertionError("expected McpProtocolError")

                connected_after_error = state["connected"]

                # A single slow request (timeout) must not mark the connection failed.
                try:
                    await call("slow")
                except McpTransportError as error:
                    timeout_error = error
                else:
                    raise AssertionError("expected request_timeout")

                connected_after_timeout = state["connected"]
                second = await call("pid")
                return (
                    pid,
                    protocol_error,
                    connected_after_error,
                    timeout_error,
                    connected_after_timeout,
                    second["content"][0]["text"],
                )

            (
                pid,
                protocol_error,
                connected_after_error,
                timeout_error,
                connected_after_timeout,
                pid_after,
            ) = run(scenario())
            run(state["client"].close())
            self.assertEqual(protocol_error.code, -32602)
            self.assertTrue(connected_after_error)
            self.assertEqual(timeout_error.code, "request_timeout")
            self.assertTrue(connected_after_timeout)
            self.assertEqual(pid, pid_after)
        finally:
            self.restore_home(root, previous)

    def test_transport_failure_still_marks_state_failed(self):
        state = {
            "config": {"name": "srv"},
            "connected": True,
            "state": "connected",
            "tools": [],
            "resources": [],
            "channelRegistration": {"requested": False, "status": "not_requested"},
        }

        class _BrokenClient:
            async def request(self, _method, _params=None):
                raise McpTransportError("transport_closed", "gone")

        state["client"] = _BrokenClient()
        with self.assertRaises(McpTransportError):
            run(_request_with_state_tracking(state, "tools/list"))
        self.assertFalse(state["connected"])

    def test_safe_request_returns_none_for_method_not_found(self):
        state = {
            "config": {"name": "srv"},
            "connected": True,
            "state": "connected",
            "tools": [],
            "resources": [],
            "channelRegistration": {"requested": False, "status": "not_requested"},
        }

        class _NoMethodClient:
            async def request(self, _method, _params=None):
                raise McpProtocolError("-32601: Method not found", code=-32601)

        state["client"] = _NoMethodClient()
        self.assertIsNone(run(_safe_request(state, "resources/list")))
        self.assertTrue(state["connected"])


class _PagedClient:
    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.calls: list[dict | None] = []

    async def request(self, _method: str, params=None):
        self.calls.append(params)
        cursor = (params or {}).get("cursor")
        if cursor is None:
            return self.pages[0]
        for index, page in enumerate(self.pages[:-1]):
            if page.get("nextCursor") == cursor:
                return self.pages[index + 1]
        return self.pages[-1]


def _paged_state(client) -> dict:
    return {
        "config": {"name": "paged"},
        "connected": True,
        "state": "connected",
        "tools": [],
        "resources": [],
        "client": client,
        "channelRegistration": {"requested": False, "status": "not_requested"},
    }


class McpListPaginationTests(AgentCoreTestCase):
    def test_tools_list_aggregates_every_page(self):
        client = _PagedClient(
            [
                {"tools": [{"name": "one", "inputSchema": {"type": "object"}}], "nextCursor": "a"},
                {"tools": [{"name": "two", "inputSchema": {"type": "object"}}], "nextCursor": "b"},
                {"tools": [{"name": "three", "inputSchema": {"type": "object"}}]},
            ]
        )
        tools = run(_refresh_tools(_paged_state(client)))
        self.assertEqual([tool["name"] for tool in tools], ["one", "two", "three"])
        self.assertEqual(client.calls, [None, {"cursor": "a"}, {"cursor": "b"}])

    def test_resources_list_aggregates_every_page(self):
        client = _PagedClient(
            [
                {"resources": [{"uri": "mem://a", "name": "a"}], "nextCursor": "next"},
                {"resources": [{"uri": "mem://b", "name": "b"}]},
            ]
        )
        resources = run(_refresh_resources(_paged_state(client)))
        self.assertEqual([item["uri"] for item in resources], ["mem://a", "mem://b"])

    def test_repeated_cursor_terminates_pagination(self):
        class _LoopingClient:
            def __init__(self):
                self.calls = 0

            async def request(self, _method, _params=None):
                self.calls += 1
                return {
                    "tools": [{"name": f"tool{self.calls}", "inputSchema": {"type": "object"}}],
                    "nextCursor": "same",
                }

        client = _LoopingClient()
        tools = run(_refresh_tools(_paged_state(client)))
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(tools), 2)

    def test_unsupported_tools_list_still_returns_empty(self):
        class _NoToolsClient:
            async def request(self, _method, _params=None):
                raise McpProtocolError("-32601: Method not found", code=-32601)

        self.assertEqual(run(_refresh_tools(_paged_state(_NoToolsClient()))), [])
