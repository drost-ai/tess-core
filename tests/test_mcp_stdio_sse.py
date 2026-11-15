from __future__ import annotations

from tests.support.tess_core_case import *


class McpStdioSseTests(TessCoreTestCase):
    def test_stdio_mcp_runtime_discovers_tools_and_resources(self):
        root, home, workspace, previous = self.temp_context()
        try:
            server = workspace / "fake_mcp.py"
            server.write_text(
                textwrap.dedent(
                    r'''
                    import json
                    import sys

                    def read_message():
                        while True:
                            line = sys.stdin.buffer.readline()
                            if not line:
                                return None
                            if line.strip():
                                return json.loads(line.decode())

                    def send(message):
                        sys.stdout.buffer.write(json.dumps(message).encode() + b"\n")
                        sys.stdout.buffer.flush()

                    while True:
                        request = read_message()
                        if request is None:
                            break
                        method = request.get("method")
                        request_id = request.get("id")
                        if request_id is None:
                            continue
                        if method == "initialize":
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"serverInfo": {"name": "fake"}}})
                        elif method == "tools/list":
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "echo", "description": "Echo input", "inputSchema": {"type": "object"}}, {"name": "raw", "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}}}]}})
                        elif method == "tools/call":
                            args = request.get("params", {}).get("arguments", {})
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": "echo:" + args.get("value", "")}]}})
                        elif method == "resources/list":
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"resources": [{"uri": "memory://one", "name": "one", "mimeType": "text/plain"}, {"uri": "memory://missing-name"}]}})
                        elif method == "resources/read":
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"contents": [{"uri": "memory://one", "mimeType": "text/plain", "text": "resource text"}]}})
                        else:
                            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                    '''
                )
            )
            runtime = run(core.create_mcp_runtime({"cwd": str(workspace), "servers": [{"name": "fake", "transport": "stdio", "commandOrUrl": sys.executable, "args": [str(server)], "updatedAt": "now"}]}))
            self.assertEqual(runtime["listTools"]()[0]["runtimeName"], "mcp__fake__echo")
            raw_tool = next(tool for tool in runtime["listTools"]() if tool["name"] == "raw")
            self.assertEqual(raw_tool["inputSchema"]["properties"]["value"]["type"], "string")
            raw_definition = next(tool for tool in runtime["createToolDefinitions"]() if tool.name == "mcp__fake__raw")
            self.assertEqual(raw_definition.description, "MCP fake tool raw")
            called = run(runtime["callTool"]("fake", "echo", {"value": "ok"}))
            self.assertEqual(called["content"], "echo:ok")
            resources = run(runtime["listResources"]("fake"))
            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0]["uri"], "memory://one")
            resource = run(runtime["readResource"]("fake", "memory://one"))
            self.assertEqual(resource["contents"][0]["text"], "resource text")
            run(runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_sse_mcp_runtime_discovers_tools_and_handles_notifications(self):
        root, home, workspace, previous = self.temp_context()
        stop_event = threading.Event()
        queues = []
        queues_lock = threading.Lock()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                if self.path != "/sse":
                    self.send_error(404)
                    return
                event_queue = queue.Queue()
                with queues_lock:
                    queues.append(event_queue)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()
                endpoint = f"http://127.0.0.1:{self.server.server_address[1]}/messages"
                self.wfile.write(f"event: endpoint\ndata: {json.dumps({'url': endpoint})}\n\n".encode())
                self.wfile.flush()
                try:
                    while not stop_event.is_set():
                        try:
                            message = event_queue.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        self.wfile.write(f"data: {json.dumps(message)}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                if self.path != "/messages":
                    self.send_error(404)
                    return
                length = int(self.headers.get("content-length") or 0)
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                request_id = request.get("id")
                method = request.get("method")
                result = {}
                if method == "initialize":
                    result = {"serverInfo": {"name": "sse-fake"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "echo", "description": "Echo input", "inputSchema": {"type": "object"}}]}
                elif method == "tools/call":
                    args = request.get("params", {}).get("arguments", {})
                    result = {"content": [{"type": "text", "text": "sse:" + args.get("value", "")}]}
                elif method == "resources/list":
                    result = {"resources": [{"uri": "memory://sse", "name": "sse", "mimeType": "text/plain"}]}
                elif method == "resources/read":
                    result = {"contents": [{"uri": "memory://sse", "mimeType": "text/plain", "text": "sse resource"}]}
                if request_id is not None:
                    with queues_lock:
                        current_queues = list(queues)
                    for item in current_queues:
                        item.put({"jsonrpc": "2.0", "id": request_id, "result": result})
                        if method == "initialize":
                            item.put({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "text": "ready"}})
                            item.put({"jsonrpc": "2.0", "method": "notifications/progress", "params": "halfway"})
                            item.put({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {"reason": "new-tool"}})
                            item.put({"jsonrpc": "2.0", "method": "notifications/resources/list_changed", "params": {"reason": "new-resource"}})
                            item.put({"jsonrpc": "2.0", "method": "notifications/logging/message", "params": {"level": "warning", "data": "log-ready"}})
                self.send_response(202)
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = None
        try:
            events = []
            url = f"http://127.0.0.1:{server.server_address[1]}/sse"
            runtime = run(
                core.create_mcp_runtime(
                    {
                        "cwd": str(workspace),
                        "servers": [{"name": "sse", "transport": "sse", "commandOrUrl": url, "updatedAt": "now"}],
                        "onRuntimeEvent": lambda event, data: events.append((event, data)),
                    }
                )
            )
            self.assertEqual(runtime["listTools"]()[0]["runtimeName"], "mcp__sse__echo")
            called = run(runtime["callTool"]("sse", "echo", {"value": "ok"}))
            self.assertEqual(called["content"], "sse:ok")
            resource = run(runtime["readResource"]("sse", "memory://sse"))
            self.assertEqual(resource["contents"][0]["text"], "sse resource")
            for _ in range(20):
                if any(event == "mcp.channel_notification" for event, _ in events):
                    break
                time.sleep(0.05)
            self.assertTrue(any(event == "mcp.channel_notification" and data["server"] == "sse" for event, data in events))
            self.assertTrue(any(event == "mcp.channel_notification" and data["method"] == "notifications/progress" and data["params"] == {"payload": "halfway"} for event, data in events))
            self.assertTrue(any(event == "mcp.tools_changed" and data["server"] == "sse" for event, data in events))
            self.assertTrue(any(event == "mcp.resources_changed" and data["server"] == "sse" for event, data in events))
            self.assertTrue(any(event == "mcp.log" and data["level"] == "warning" and data["message"] == "log-ready" for event, data in events))
            status = runtime["listServerStatus"]()[0]
            self.assertEqual(status["serverInfo"]["name"], "sse-fake")
            self.assertIsNotNone(status["lastNotificationAt"])
        finally:
            if runtime:
                run(runtime["close"]())
            stop_event.set()
            server.shutdown()
            server.server_close()
            self.restore_home(root, previous)
