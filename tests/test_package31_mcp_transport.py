from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.mcp.sse_decoder import McpSseDecoder, parse_sse_events
from agent_core.mcp.stdio_decoder import McpStdioFrameDecoder
from agent_core.mcp.stdio_transport import StdioMcpClient
from agent_core.mcp.transport_policy import (
    MCP_FRAME_MAX_BYTES,
    MCP_PENDING_REQUEST_MAX,
    MCP_SSE_EVENT_MAX_BYTES,
    McpTransportError,
)
from agent_core.mcp.transport_shared import BoundedNotificationQueue


def _stdio_config(name: str, mode: str, server: Path, **overrides):
    return {
        "name": name,
        "transport": "stdio",
        "commandOrUrl": sys.executable,
        "args": [str(server)],
        "env": {"MCP_MODE": mode, "MCP_PID_FILE": str(server.parent / f"{name}.pid")},
        "startupTimeoutMs": 400,
        "requestTimeoutMs": 300,
        **overrides,
    }


def _write_stdio_fixture(workspace: Path) -> Path:
    target = workspace / "mcp_adversarial.py"
    target.write_text(
        textwrap.dedent(
            r'''
            import json
            import os
            import signal
            import subprocess
            import sys
            import time

            mode = os.environ.get("MCP_MODE", "healthy")
            pid_file = os.environ.get("MCP_PID_FILE")
            if pid_file:
                with open(pid_file, "w", encoding="utf-8") as handle:
                    handle.write(str(os.getpid()))
            if mode == "stubborn":
                signal.signal(signal.SIGTERM, lambda *_args: None)

            def read_message():
                while True:
                    line = sys.stdin.buffer.readline()
                    if not line:
                        return None
                    if line.strip():
                        return json.loads(line.decode("utf-8"))

            def frame(message):
                return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"

            def send(message):
                value = frame(message)
                if mode == "fragmented":
                    for byte in value:
                        sys.stdout.buffer.write(bytes([byte]))
                        sys.stdout.buffer.flush()
                else:
                    sys.stdout.buffer.write(value)
                    sys.stdout.buffer.flush()

            emitted = False
            while True:
                request = read_message()
                if request is None:
                    break
                request_id = request.get("id")
                method = request.get("method")
                if mode == "silent":
                    continue
                if method == "initialize":
                    if mode == "oversize_line":
                        sys.stdout.buffer.write(b"x" * (8 * 1024 * 1024 + 1))
                        sys.stdout.buffer.flush()
                        continue
                    if mode == "invalid_utf8":
                        sys.stdout.buffer.write(b"\xc3(\n")
                        sys.stdout.buffer.flush()
                        continue
                    if mode == "string_id":
                        send({"jsonrpc": "2.0", "id": str(request_id), "result": {}})
                        continue
                    if mode == "invalid_json":
                        sys.stdout.buffer.write(b"{not-json\n")
                        sys.stdout.buffer.flush()
                        continue
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}, "resources": {}},
                            "serverInfo": {"name": "fixture", "version": "1"},
                        },
                    })
                elif method == "notifications/initialized":
                    continue
                elif method == "tools/list":
                    if mode == "silent_tools":
                        continue
                    if mode == "burst" and not emitted:
                        emitted = True
                        output = bytearray()
                        for index in range(400):
                            output.extend(frame({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"index": index}}))
                        output.extend(frame({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}))
                        output.extend(frame({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "alpha", "inputSchema": {"type": "object"}}]}}))
                        sys.stdout.buffer.write(output)
                        sys.stdout.buffer.flush()
                    else:
                        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "alpha", "inputSchema": {"type": "object"}}]}})
                elif method == "resources/list":
                    if mode == "silent_resources":
                        continue
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}})
                elif method == "tools/call":
                    if mode == "hang_call":
                        continue
                    if mode == "exit_call":
                        sys.stderr.write("x" * (20 * 1024) + "TAIL_MARKER")
                        sys.stderr.flush()
                        raise SystemExit(23)
                    if mode == "orphan_descendant":
                        ready_file = pid_file + ".descendant.ready"
                        descendant = subprocess.Popen([
                            sys.executable,
                            "-c",
                            "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open(sys.argv[1], 'w').write('ready'); time.sleep(300)",
                            ready_file,
                        ])
                        with open(pid_file + ".descendant", "w", encoding="utf-8") as handle:
                            handle.write(str(descendant.pid))
                        deadline = time.monotonic() + 1
                        while not os.path.exists(ready_file) and time.monotonic() < deadline:
                            time.sleep(0.005)
                        raise SystemExit(24)
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": "called alpha"}]}})
                elif request_id is not None:
                    send({"jsonrpc": "2.0", "id": request_id, "result": {}})

            if mode == "stubborn":
                while True:
                    time.sleep(1)
            '''
        ),
        encoding="utf-8",
    )
    return target


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class _Headers(dict):
    def get_all(self, name):
        value = next((item for key, item in self.items() if key.lower() == name.lower()), None)
        return None if value is None else value if isinstance(value, list) else [value]

    def raw_items(self):
        for key, value in self.items():
            if isinstance(value, list):
                for item in value:
                    yield key, item
            else:
                yield key, value


class _Response:
    status = 200

    def __init__(self, chunks=(), headers=None, error=None):
        self.chunks = list(chunks)
        self.headers = _Headers(headers or {})
        self.error = error
        self.closed = False

    def read(self, _size=0):
        if self.error:
            raise self.error
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class Package31McpTransportTests(AgentCoreTestCase):
    def test_stdio_decoder_exact_limits_fragmentation_and_invalid_frames(self):
        payload = b" " * MCP_FRAME_MAX_BYTES
        decoder = McpStdioFrameDecoder()
        frame = payload + b"\n"
        output = []
        for offset in range(0, len(frame), 65_537):
            output.extend(decoder.push(frame[offset : offset + 65_537]))
        self.assertEqual(len(output), 1)
        self.assertEqual(len(output[0].encode()), MCP_FRAME_MAX_BYTES)

        decoder = McpStdioFrameDecoder()
        frames = decoder.push(b'{"a":1}\r\n\n{"b":2}\npartial')
        self.assertEqual(frames, ['{"a":1}', '{"b":2}'])
        self.assertEqual(decoder.bufferedBytes, len(b"partial"))
        self.assertEqual(decoder.push(b"\n"), ["partial"])

        invalid = (
            (b"x" * (MCP_FRAME_MAX_BYTES + 1), "frame_limit"),
            (b"\xc3(\n", "invalid_encoding"),
        )
        for raw, code in invalid:
            with self.subTest(code=code):
                with self.assertRaises(McpTransportError) as raised:
                    McpStdioFrameDecoder().push(raw)
                self.assertEqual(raised.exception.code, code)

    def test_sse_crlf_comments_multiline_final_eof_and_event_limit(self):
        raw = ': comment\r\nevent: result\r\ndata: {"jsonrpc":"2.0",\r\ndata: "id":1}\r\n'
        decoder = McpSseDecoder()
        events = []
        encoded = raw.encode()
        for byte in encoded:
            events.extend(decoder.push(bytes([byte])))
        events.extend(decoder.push(b"", final=True))
        self.assertEqual(
            events,
            [{"event": "result", "data": '{"jsonrpc":"2.0",\n"id":1}'}],
        )
        self.assertEqual(json.loads(events[0]["data"])["id"], 1)
        with self.assertRaises(McpTransportError) as raised:
            parse_sse_events(f"data: {'x' * MCP_SSE_EVENT_MAX_BYTES}\n\n")
        self.assertEqual(raised.exception.code, "event_limit")

    def test_notification_queue_is_bounded_and_preserves_catalog_signal(self):
        observed = []
        notifications = BoundedNotificationQueue(observed.append)
        for index in range(400):
            notifications.push({"method": "notifications/progress", "params": {"index": index}})
        notifications.push({"method": "notifications/tools/list_changed"})
        notifications.push({"method": "notifications/tools/list_changed"})
        notifications.flush()
        self.assertLessEqual(len(observed), 256)
        self.assertEqual(
            sum(item["method"] == "notifications/tools/list_changed" for item in observed),
            1,
        )

    def test_silent_startup_isolated_and_discovery_is_inside_deadline(self):
        root, _home, workspace, previous = self.temp_context()
        runtime = None
        try:
            server = _write_stdio_fixture(workspace)
            started = time.monotonic()
            runtime = run(
                core.create_mcp_runtime(
                    cwd=str(workspace),
                    servers=[
                        _stdio_config("silent", "silent", server, startupTimeoutMs=120),
                        _stdio_config("healthy", "fragmented", server),
                    ],
                )
            )
            self.assertLess(time.monotonic() - started, 1.5)
            statuses = {item["name"]: item for item in runtime["listServerStatus"]()}
            self.assertTrue(statuses["healthy"]["connected"])
            self.assertEqual(statuses["silent"]["state"], "unavailable")
            self.assertIn("startup_timeout", statuses["silent"]["error"])
            self.assertIn(
                "called alpha",
                run(runtime["callTool"]("healthy", "alpha", {}))["content"],
            )
            run(runtime["close"]())
            runtime = None

            for mode in ("silent_tools", "silent_resources"):
                runtime = run(
                    core.create_mcp_runtime(
                        cwd=str(workspace),
                        servers=[
                            _stdio_config(
                                mode,
                                mode,
                                server,
                                startupTimeoutMs=120,
                                requestTimeoutMs=1_000,
                            )
                        ],
                    )
                )
                status = runtime["listServerStatus"]()[0]
                self.assertEqual(status["state"], "unavailable")
                self.assertIn("startup_timeout", status["error"])
                run(runtime["close"]())
                runtime = None
        finally:
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_stdio_transport_faults_do_not_retain_children(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            server = _write_stdio_fixture(workspace)
            for mode, code in (
                ("oversize_line", "frame_limit"),
                ("invalid_utf8", "invalid_encoding"),
                ("string_id", "invalid_framing"),
                ("invalid_json", "invalid_framing"),
            ):
                with self.subTest(mode=mode):
                    runtime = run(
                        core.create_mcp_runtime(
                            cwd=str(workspace),
                            servers=[_stdio_config(mode, mode, server)],
                        )
                    )
                    status = runtime["listServerStatus"]()[0]
                    self.assertFalse(status["connected"])
                    self.assertIn(code, status["error"])
                    run(runtime["close"]())
                    pid = int((workspace / f"{mode}.pid").read_text())
                    deadline = time.monotonic() + 1
                    while _pid_alive(pid) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(_pid_alive(pid))
        finally:
            self.restore_home(root, previous)

    def test_stdio_deadline_close_and_bounded_stderr_tail(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            server = _write_stdio_fixture(workspace)
            runtime = run(
                core.create_mcp_runtime(
                    cwd=str(workspace),
                    servers=[
                        _stdio_config(
                            "hang",
                            "hang_call",
                            server,
                            requestTimeoutMs=50,
                        )
                    ],
                )
            )
            with self.assertRaises(McpTransportError) as raised:
                run(runtime["callTool"]("hang", "alpha", {}))
            self.assertEqual(raised.exception.code, "request_timeout")
            run(runtime["close"]())

            runtime = run(
                core.create_mcp_runtime(
                    cwd=str(workspace),
                    servers=[_stdio_config("exit", "exit_call", server)],
                )
            )
            with self.assertRaises(RuntimeError) as raised:
                run(runtime["callTool"]("exit", "alpha", {}))
            self.assertIn("code 23", str(raised.exception))
            self.assertIn("TAIL_MARKER", str(raised.exception))
            self.assertLess(len(str(raised.exception).encode()), 17 * 1024)
            run(runtime["close"]())

            runtime = run(
                core.create_mcp_runtime(
                    cwd=str(workspace),
                    servers=[_stdio_config("stubborn", "stubborn", server)],
                )
            )
            pid = int((workspace / "stubborn.pid").read_text())
            started = time.monotonic()
            run(runtime["close"]())
            self.assertLess(time.monotonic() - started, 3)
            self.assertFalse(_pid_alive(pid))
            with self.assertRaisesRegex(RuntimeError, "lifecycle is closed"):
                run(runtime["callTool"]("stubborn", "alpha", {}))
        finally:
            self.restore_home(root, previous)

    def test_stdio_pending_ceiling_and_close_settle_once(self):
        async def scenario():
            client = StdioMcpClient(
                {"name": "fake", "transport": "stdio", "commandOrUrl": "unused"},
                "/tmp",
            )
            client._write_message = lambda _request: None
            pending = [
                asyncio.create_task(
                    client._send(
                        {"jsonrpc": "2.0", "id": index, "method": "tools/call"}
                    )
                )
                for index in range(MCP_PENDING_REQUEST_MAX)
            ]
            deadline = time.monotonic() + 1
            while len(client.pending) < MCP_PENDING_REQUEST_MAX and time.monotonic() < deadline:
                await asyncio.sleep(0)
            self.assertEqual(len(client.pending), MCP_PENDING_REQUEST_MAX)
            with self.assertRaises(McpTransportError) as raised:
                await client._send(
                    {"jsonrpc": "2.0", "id": 999, "method": "tools/call"}
                )
            self.assertEqual(raised.exception.code, "pending_limit")
            await client.close()
            outcomes = await asyncio.gather(*pending, return_exceptions=True)
            self.assertTrue(
                all(
                    isinstance(item, McpTransportError)
                    and item.code == "transport_closed"
                    for item in outcomes
                )
            )

        run(scenario())

    def test_exiting_stdio_parent_cannot_strand_stubborn_descendant(self):
        root, _home, workspace, previous = self.temp_context()
        runtime = None
        try:
            server = _write_stdio_fixture(workspace)
            runtime = run(
                core.create_mcp_runtime(
                    cwd=str(workspace),
                    servers=[
                        _stdio_config(
                            "orphan",
                            "orphan_descendant",
                            server,
                            requestTimeoutMs=3_000,
                        )
                    ],
                )
            )
            with self.assertRaisesRegex(RuntimeError, "code 24"):
                run(runtime["callTool"]("orphan", "alpha", {}))
            descendant = int((workspace / "orphan.pid.descendant").read_text())
            deadline = time.monotonic() + 3
            while _pid_alive(descendant) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(_pid_alive(descendant))
        finally:
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

if __name__ == "__main__":
    unittest.main()
