from __future__ import annotations

import gzip
import socket

from tests.support.agent_core_case import *
from tests.test_package31_mcp_transport import _Response

from agent_core.mcp.http_body import (
    _BoundedHeaderReader,
    _close_http_response,
    _parse_declared_length,
    iterateMcpBody,
    validateMcpResponseHeaders,
)
from agent_core.mcp.http_transport import HttpMcpClient
from agent_core.mcp.auth import McpOAuthRuntime
from agent_core.mcp.sse_transport import SseMcpClient
from agent_core.mcp.transport_policy import (
    MCP_DECODED_BODY_MAX_BYTES,
    MCP_FRAME_MAX_BYTES,
    MCP_HEADER_MAX_BYTES,
    MCP_NOTIFICATION_QUEUE_MAX,
    MCP_PENDING_REQUEST_MAX,
    McpTransportError,
    resolveMcpTransportPolicy,
)


class Package31McpHttpTransportTests(AgentCoreTestCase):
    def test_http_header_acquisition_and_declared_length_are_bounded(self):
        reader = _BoundedHeaderReader(
            io.BytesIO(
                b"HTTP/1.1 200 OK\r\n"
                + b"x: "
                + b"a" * MCP_HEADER_MAX_BYTES
                + b"\r\n"
            )
        )
        self.assertEqual(reader.readline(), b"HTTP/1.1 200 OK\r\n")
        with self.assertRaises(McpTransportError) as raised:
            reader.readline()
        self.assertEqual(raised.exception.code, "header_limit")
        with self.assertRaises(McpTransportError) as raised:
            _parse_declared_length("9007199254740992")
        self.assertEqual(raised.exception.code, "invalid_framing")

    def test_http_headers_compressed_decoded_idle_and_content_limits(self):
        policy = resolveMcpTransportPolicy({"bodyIdleTimeoutMs": 25})
        with self.assertRaises(McpTransportError) as raised:
            validateMcpResponseHeaders(
                _Response(headers={"x-large": "x" * MCP_HEADER_MAX_BYTES})
            )
        self.assertEqual(raised.exception.code, "header_limit")
        with self.assertRaises(McpTransportError) as raised:
            validateMcpResponseHeaders(
                _Response(headers={"content-length": str(MCP_FRAME_MAX_BYTES + 1)})
            )
        self.assertEqual(raised.exception.code, "frame_limit")
        compressed = gzip.compress(b"a" * (MCP_DECODED_BODY_MAX_BYTES + 1))
        with self.assertRaises(McpTransportError) as raised:
            list(
                iterateMcpBody(
                    _Response(
                        [compressed],
                        {
                            "content-encoding": "gzip",
                            "content-length": str(len(compressed)),
                        },
                    ),
                    policy,
                )
            )
        self.assertEqual(raised.exception.code, "decoded_limit")
        with self.assertRaises(McpTransportError) as raised:
            list(iterateMcpBody(_Response(error=socket.timeout()), policy))
        self.assertEqual(raised.exception.code, "body_idle_timeout")

    def test_continuous_http_body_uses_incremental_reader(self):
        class StreamingResponse:
            headers = {}

            def __init__(self):
                self.values = [b"event: endpoint\n", b"data: /messages\n\n", b""]
                self.closed = False

            def read(self, _size=0):
                raise AssertionError("continuous acquisition used blocking read")

            def read1(self, _size=0):
                return self.values.pop(0)

            def close(self):
                self.closed = True

        response = StreamingResponse()
        chunks = list(
            iterateMcpBody(
                response,
                resolveMcpTransportPolicy(),
                continuous=True,
            )
        )
        self.assertEqual(chunks, [b"event: endpoint\n", b"data: /messages\n\n"])
        self.assertTrue(response.closed)

    def test_http_close_shuts_down_stream_socket_before_closing(self):
        class StreamSocket:
            def __init__(self):
                self.calls = []

            def shutdown(self, how):
                self.calls.append(how)

        stream_socket = StreamSocket()

        class Response:
            fp = type("Stream", (), {"raw": type("Raw", (), {"_sock": stream_socket})()})()

            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        response = Response()
        _close_http_response(response)
        self.assertEqual(stream_socket.calls, [socket.SHUT_RDWR])
        self.assertTrue(response.closed)

    def test_http_sse_notifications_flush_bounded_and_json_is_strict(self):
        observed = []
        client = HttpMcpClient(
            {
                "name": "sse-response",
                "transport": "http",
                "commandOrUrl": "https://example.test/mcp",
            },
            "/tmp",
            observed.append,
        )
        events = [
            f'data: {{"jsonrpc":"2.0","method":"notifications/progress","params":{{"index":{index}}}}}\n\n'
            for index in range(400)
        ]
        events.extend(
            [
                'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n\n',
                'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n',
            ]
        )
        response = _Response(
            ["".join(events).encode()],
            {"content-type": "text/event-stream"},
        )
        self.assertEqual(client._parse_rpc_response(response, 1)["result"], {"ok": True})
        self.assertLessEqual(len(observed), MCP_NOTIFICATION_QUEUE_MAX)
        self.assertEqual(
            sum(item["method"] == "notifications/tools/list_changed" for item in observed),
            1,
        )
        with self.assertRaises(McpTransportError) as raised:
            client._parse_rpc_response(
                _Response(
                    [b'data: {"jsonrpc":"2.0","id":NaN,"result":{}}\n\n'],
                    {"content-type": "text/event-stream"},
                ),
                1,
            )
        self.assertEqual(raised.exception.code, "invalid_framing")

    def test_legacy_sse_empty_ack_routes_stream_response_and_rejects_wrong_id(self):
        async def scenario():
            client = SseMcpClient(
                {
                    "name": "legacy-sse",
                    "transport": "sse",
                    "commandOrUrl": "https://example.test/sse",
                    "requestTimeoutMs": 500,
                },
                "/tmp",
            )

            async def empty_ack(_client, request):
                return {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {},
                    "_mcp_sse_empty_ack": True,
                }

            original_send = HttpMcpClient._send
            HttpMcpClient._send = empty_ack
            try:
                pending = asyncio.create_task(client.request("tools/list"))
                while not client.pending:
                    await asyncio.sleep(0)
                client._consume_stream_event(
                    {"data": '{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'},
                    "https://example.test/sse",
                    None,
                )
                self.assertEqual(await pending, {"tools": []})

                mismatched = asyncio.create_task(client.request("resources/list"))
                while not client.pending:
                    await asyncio.sleep(0)
                client._consume_stream_event(
                    {"data": '{"jsonrpc":"2.0","id":999,"result":{}}'},
                    "https://example.test/sse",
                    None,
                )
                outcome = (await asyncio.gather(mismatched, return_exceptions=True))[0]
                self.assertIsInstance(outcome, McpTransportError)
                self.assertEqual(outcome.code, "invalid_framing")
            finally:
                HttpMcpClient._send = original_send
                await client.close()

        run(scenario())

    def test_http_pending_close_and_sse_origin_are_fail_closed(self):
        async def scenario():
            opened = threading.Event()
            released = threading.Event()

            class BlockingResponse(_Response):
                def __init__(self):
                    super().__init__(headers={"content-type": "application/json"})

                def read(self, _size=0):
                    opened.set()
                    released.wait(timeout=2)
                    raise OSError("closed")

                def close(self):
                    self.closed = True
                    released.set()

            def fake_urlopen(_request, timeout=None):
                return BlockingResponse()

            client = HttpMcpClient(
                {
                    "name": "http",
                    "transport": "http",
                    "commandOrUrl": "https://example.test/mcp",
                    "requestTimeoutMs": 5_000,
                },
                "/tmp",
            )
            with mock.patch.object(mcp_module.http_body, "urlopen", fake_urlopen):
                pending = asyncio.create_task(client.request("tools/list"))
                await asyncio.to_thread(opened.wait, 1)
                await client.close()
                outcome = (await asyncio.gather(pending, return_exceptions=True))[0]
                self.assertIsInstance(outcome, McpTransportError)
                self.assertEqual(outcome.code, "transport_closed")
                await client.close()

            sse = SseMcpClient(
                {
                    "name": "sse",
                    "transport": "sse",
                    "commandOrUrl": "https://example.test/sse",
                },
                "/tmp",
            )
            sse._set_endpoint("/messages", "https://example.test/sse")
            self.assertEqual(sse.post_url, "https://example.test/messages")
            with self.assertRaises(McpTransportError) as raised:
                sse._set_endpoint(
                    "https://attacker.test/messages",
                    "https://example.test/sse",
                )
            self.assertEqual(raised.exception.code, "invalid_framing")
            await sse.close()

        run(scenario())

    def test_http_total_connect_header_and_content_type_deadlines(self):
        async def scenario():
            release = threading.Event()

            def blocked_urlopen(_request, timeout=None):
                release.wait(timeout=2)
                return _Response(
                    [b'{"jsonrpc":"2.0","id":1,"result":{}}'],
                    {"content-type": "application/json"},
                )

            client = HttpMcpClient(
                {
                    "name": "total",
                    "transport": "http",
                    "commandOrUrl": "https://example.test/mcp",
                    "requestTimeoutMs": 25,
                },
                "/tmp",
            )
            with mock.patch.object(mcp_module.http_body, "urlopen", blocked_urlopen):
                with self.assertRaises(McpTransportError) as raised:
                    await client.request("tools/list")
                self.assertEqual(raised.exception.code, "request_timeout")
                release.set()
                await client.close()

            class DeadlineConnection:
                mode = "connect"

                def __init__(self, *_args, **_kwargs):
                    self.sock = self
                    self.closed = False
                    self.response_class = None

                def connect(self):
                    if self.mode == "connect":
                        raise socket.timeout()

                def settimeout(self, _timeout):
                    return None

                def putrequest(self, *_args, **_kwargs):
                    return None

                def putheader(self, *_args):
                    return None

                def endheaders(self, _body=None):
                    return None

                def getresponse(self):
                    raise socket.timeout()

                def close(self):
                    self.closed = True

            with mock.patch(
                "agent_core.mcp.http_body.HTTPConnection",
                DeadlineConnection,
            ):
                for mode, code in (("connect", "connect_timeout"), ("headers", "headers_timeout")):
                    DeadlineConnection.mode = mode
                    client = HttpMcpClient(
                        {
                            "name": mode,
                            "transport": "http",
                            "commandOrUrl": "http://example.test/mcp",
                            "requestTimeoutMs": 1_000,
                        },
                        "/tmp",
                    )
                    with self.assertRaises(McpTransportError) as raised:
                        await client.request("tools/list")
                    self.assertEqual(raised.exception.code, code)
                    await client.close()

            client = HttpMcpClient(
                {
                    "name": "type",
                    "transport": "http",
                    "commandOrUrl": "https://example.test/mcp",
                },
                "/tmp",
            )
            with mock.patch.object(
                mcp_module.http_body,
                "urlopen",
                lambda *_args, **_kwargs: _Response(
                    [b"not json"], {"content-type": "text/plain"}
                ),
            ):
                with self.assertRaises(McpTransportError) as raised:
                    await client.request("tools/list")
                self.assertEqual(raised.exception.code, "invalid_content_type")
            await client.close()

        run(scenario())

    def test_http_pending_ceiling_is_atomic_with_close(self):
        async def scenario():
            client = HttpMcpClient(
                {
                    "name": "pending",
                    "transport": "http",
                    "commandOrUrl": "https://example.test/mcp",
                },
                "/tmp",
            )
            exchanges = [
                client._admit_exchange(f"request-{index}")
                for index in range(MCP_PENDING_REQUEST_MAX)
            ]
            with self.assertRaises(McpTransportError) as raised:
                client._admit_exchange("overflow")
            self.assertEqual(raised.exception.code, "pending_limit")
            for exchange in exchanges:
                exchange.release()
            await client.close()
            with self.assertRaises(McpTransportError) as raised:
                client._admit_exchange("late")
            self.assertEqual(raised.exception.code, "transport_closed")

        run(scenario())

    def test_oauth_startup_cancellation_and_close_release_listener(self):
        async def exercise(cancel: bool):
            class Listener:
                latest = None

                def __init__(self):
                    self.port = 49152
                    self.waiting = asyncio.Event()
                    self.released = asyncio.Event()
                    self.closed = False
                    Listener.latest = self

                @classmethod
                async def start(cls, _port):
                    return cls()

                async def wait_for_code(self, _state, _timeout):
                    self.waiting.set()
                    await self.released.wait()
                    raise RuntimeError("listener closed")

                async def close(self):
                    self.closed = True
                    self.released.set()

            oauth = McpOAuthRuntime(
                config={
                    "name": "oauth",
                    "transport": "http",
                    "commandOrUrl": "https://example.test/mcp",
                    "oauth": {"redirect": {"mode": "loopback", "timeoutSeconds": 300}},
                },
                store=object(),
            )
            oauth._discover_authorization_server = mock.AsyncMock(
                return_value={
                    "authorization_endpoint": "https://issuer.test/authorize",
                    "token_endpoint": "https://issuer.test/token",
                }
            )
            oauth._ensure_client = mock.AsyncMock(return_value={"clientId": "client"})
            with mock.patch(
                "agent_core.mcp.auth.LoopbackListener",
                Listener,
            ):
                task = asyncio.create_task(
                    oauth._authorize(None, "https://example.test/mcp")
                )
                while Listener.latest is None:
                    await asyncio.sleep(0)
                await Listener.latest.waiting.wait()
                if cancel:
                    task.cancel()
                else:
                    await oauth.close()
                await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(Listener.latest.closed)

        run(exercise(True))
        run(exercise(False))


if __name__ == "__main__":
    unittest.main()
