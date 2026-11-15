from __future__ import annotations

from tests.support.tess_core_case import *

import socket
import uuid

from tests.test_mcp_interop_quickwins import _RawHttpServer, _http_config, _json_response

from tess_core.mcp.http_transport import HttpMcpClient
from tess_core.mcp.state import (
    _connect_state,
    _create_state,
    _ensure_connected,
    _request_with_state_tracking,
)
from tess_core.mcp.stdio_transport import StdioMcpClient
from tess_core.mcp.transport_policy import McpTransportError
from tess_core.mcp.transport_shared import _validate_transport_message


def _noop_event(*_args, **_kwargs) -> None:
    return None


_INIT_RESULT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "session-fixture", "version": "1"},
}


def _accepted(conn: socket.socket) -> None:
    conn.sendall(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")


class _SessionServer:
    """Stateful Streamable HTTP server: sessions like the official SDK default."""

    def __init__(self):
        self.sessions: set[str] = set()
        self.issued: list[str] = []
        self.expired: set[str] = set()
        self.deletes: list[str | None] = []
        self.session_headers: list[tuple[str, str | None]] = []
        self.server = _RawHttpServer(self._handle)

    def _handle(self, conn: socket.socket, request: dict) -> bool:
        session = request["headers"].get("mcp-session-id")
        if request["method"] == "DELETE":
            self.deletes.append(session)
            self.sessions.discard(session)
            conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            return True
        message = json.loads(request["body"])
        rpc_method = message.get("method")
        self.session_headers.append((str(rpc_method), session))
        if rpc_method == "initialize":
            new_session = uuid.uuid4().hex
            self.sessions.add(new_session)
            self.issued.append(new_session)
            body = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": _INIT_RESULT}
            ).encode("utf-8")
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Mcp-Session-Id: " + new_session.encode() + b"\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body) + body
            )
            return True
        if session in self.expired:
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return True
        if session not in self.sessions:
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32600, "message": "Missing session ID"},
                }
            ).encode("utf-8")
            conn.sendall(
                b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body) + body
            )
            return True
        if message.get("id") is None:
            _accepted(conn)
            return True
        result = (
            {"tools": [{"name": "alpha", "inputSchema": {"type": "object"}}]}
            if rpc_method == "tools/list"
            else {}
        )
        _json_response(conn, {"jsonrpc": "2.0", "id": message["id"], "result": result})
        return True

    def close(self) -> None:
        self.server.close()


class McpSessionManagementTests(TessCoreTestCase):
    def test_session_id_is_captured_echoed_and_deleted(self):
        server = _SessionServer()
        client = HttpMcpClient(_http_config(server.server.origin), os.getcwd())

        async def scenario():
            await client.initialize()
            result = await client.request("tools/list")
            await client.close()
            return result

        try:
            result = run(scenario())
            self.assertEqual(result["tools"][0]["name"], "alpha")
            by_method = dict(server.session_headers[1:])
            self.assertIsNone(dict(server.session_headers)["initialize"])
            session = next(iter(server.sessions | set(server.deletes) - {None}))
            # Every post-initialize request carried the session header.
            self.assertEqual(by_method["notifications/initialized"], session)
            self.assertEqual(by_method["tools/list"], session)
            self.assertEqual(server.deletes, [session])
        finally:
            server.close()

    def test_expired_session_fails_with_session_expired_then_reconnects(self):
        root, home, workspace, previous = self.temp_context()
        server = _SessionServer()
        try:
            config = _http_config(server.server.origin, startupTimeoutMs=5000)
            state = _create_state(config)

            async def scenario():
                await _connect_state(state, str(workspace), _noop_event)
                assert state["connected"], state.get("error")
                first_session = next(iter(server.sessions))
                server.expired.add(first_session)
                try:
                    await _request_with_state_tracking(
                        state, "tools/call", {"name": "alpha", "arguments": {}}
                    )
                except McpTransportError as error:
                    expiry = error
                else:
                    raise AssertionError("expected session_expired")
                failed_state = state["connected"]
                await _ensure_connected(state, str(workspace), _noop_event)
                recovered = await _request_with_state_tracking(
                    state, "tools/call", {"name": "alpha", "arguments": {}}
                )
                await state["client"].close()
                return expiry, failed_state, first_session, recovered

            expiry, failed_state, first_session, recovered = run(scenario())
            self.assertEqual(expiry.code, "session_expired")
            self.assertFalse(failed_state)
            self.assertEqual(recovered, {})
            # Reconnect ran a fresh initialize and was granted a new session,
            # which the final close() then terminated via DELETE.
            self.assertEqual(len(server.issued), 2)
            self.assertEqual(server.issued[0], first_session)
            self.assertEqual(server.deletes, [server.issued[1]])
        finally:
            server.close()
            self.restore_home(root, previous)


def _write_inbound_fixture(workspace: Path) -> Path:
    target = workspace / "mcp_inbound_requests.py"
    target.write_text(
        textwrap.dedent(
            r'''
            import json
            import os
            import sys

            reply_log = os.environ["MCP_REPLY_LOG"]

            def send(message):
                sys.stdout.buffer.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
                sys.stdout.buffer.flush()

            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                message = json.loads(line.decode("utf-8"))
                request_id = message.get("id")
                method = message.get("method")
                if method is None:
                    with open(reply_log, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(message) + "\n")
                    continue
                if method == "initialize":
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "inbound", "version": "1"},
                        },
                    })
                elif method == "notifications/initialized":
                    continue
                elif method == "tools/list":
                    # Server-initiated traffic in front of the real response: a
                    # string-id ping, then a request whose id collides with the
                    # in-flight client request id.
                    send({"jsonrpc": "2.0", "id": "srv-ping", "method": "ping"})
                    send({"jsonrpc": "2.0", "id": request_id, "method": "sampling/createMessage", "params": {}})
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "alpha", "inputSchema": {"type": "object"}}]}})
                elif request_id is not None:
                    send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            '''
        ),
        encoding="utf-8",
    )
    return target


class McpInboundServerRequestTests(TessCoreTestCase):
    def test_validation_allows_string_ids_only_on_inbound_requests(self):
        message = _validate_transport_message(
            {"jsonrpc": "2.0", "id": "srv-1", "method": "ping"}, "stdio frame"
        )
        self.assertEqual(message["id"], "srv-1")
        with self.assertRaises(McpTransportError) as raised:
            _validate_transport_message(
                {"jsonrpc": "2.0", "id": "srv-1", "result": {}}, "stdio frame"
            )
        self.assertEqual(raised.exception.code, "invalid_framing")

    def test_stdio_answers_ping_and_id_collision_without_losing_response(self):
        root, home, workspace, previous = self.temp_context()
        try:
            fixture = _write_inbound_fixture(workspace)
            reply_log = workspace / "replies.jsonl"
            client = StdioMcpClient(
                {
                    "name": "inbound",
                    "transport": "stdio",
                    "commandOrUrl": sys.executable,
                    "args": [str(fixture)],
                    "env": {"MCP_REPLY_LOG": str(reply_log)},
                    "startupTimeoutMs": 5000,
                    "requestTimeoutMs": 3000,
                },
                str(workspace),
            )

            async def scenario():
                await client.initialize()
                tools = await client.request("tools/list")
                return tools

            tools = run(scenario())
            self.assertEqual(tools["tools"][0]["name"], "alpha")
            deadline = time.monotonic() + 2
            replies = []
            while time.monotonic() < deadline:
                if reply_log.exists():
                    replies = [
                        json.loads(item)
                        for item in reply_log.read_text(encoding="utf-8").splitlines()
                    ]
                    if len(replies) >= 2:
                        break
                time.sleep(0.02)
            run(client.close())
            by_id = {reply["id"]: reply for reply in replies}
            self.assertEqual(by_id["srv-ping"]["result"], {})
            collision = next(reply for reply in replies if reply["id"] != "srv-ping")
            self.assertEqual(collision["error"]["code"], -32601)
        finally:
            self.restore_home(root, previous)

    def test_http_post_stream_ping_is_answered_and_response_still_matches(self):
        replies: list[dict] = []

        def handler(conn: socket.socket, request: dict) -> bool:
            message = json.loads(request["body"])
            if "method" not in message:
                replies.append(message)
                _accepted(conn)
                return True
            if message.get("id") is None:
                _accepted(conn)
                return True
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )

            def chunk(text: str) -> None:
                data = text.encode("utf-8")
                conn.sendall(b"%x\r\n" % len(data) + data + b"\r\n")

            ping = json.dumps({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"routed": True}}
            )
            chunk(f"event: message\ndata: {ping}\n\n")
            chunk(f"event: message\ndata: {payload}\n\n")
            conn.sendall(b"0\r\n\r\n")
            return True

        server = _RawHttpServer(handler)
        client = HttpMcpClient(_http_config(server.origin), os.getcwd())
        try:
            result = run(client.request("tools/list"))
            self.assertEqual(result, {"routed": True})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not replies:
                time.sleep(0.02)
            run(client.close())
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0], {"jsonrpc": "2.0", "id": "srv-1", "result": {}})
        finally:
            server.close()
