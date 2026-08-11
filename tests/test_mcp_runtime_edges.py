from __future__ import annotations

from tests.support.agent_core_case import *


class McpRuntimeEdgeTests(AgentCoreTestCase):
    def test_mcp_runtime_marks_degraded_and_reconnects_after_request_failure(self):
        root, home, workspace, previous = self.temp_context()
        calls = {"tool": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                method = request.get("method")
                if method == "tools/call" and calls["tool"] == 0:
                    calls["tool"] += 1
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"temporary failure")
                    return
                result = {}
                if method == "initialize":
                    result = {"serverInfo": {"name": "http-fake"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "echo", "description": "Echo input", "inputSchema": {"type": "object"}}]}
                elif method == "resources/list":
                    result = {"resources": []}
                elif method == "tools/call":
                    calls["tool"] += 1
                    result = {"content": [{"type": "text", "text": "reconnected"}]}
                payload = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = None
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/rpc"
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [{"name": "http", "transport": "http", "commandOrUrl": url, "updatedAt": "now"}]}))
            with self.assertRaises(Exception):
                run(runtime["callTool"]("http", "echo", {"value": "fail"}))
            status = runtime["listServerStatus"]()[0]
            self.assertFalse(status["connected"])
            self.assertEqual(status["state"], "degraded")
            called = run(runtime["callTool"]("http", "echo", {"value": "ok"}))
            self.assertEqual(called["content"], "reconnected")
            self.assertEqual(runtime["listServerStatus"]()[0]["state"], "connected")
        finally:
            if runtime:
                run(runtime["close"]())
            server.shutdown()
            server.server_close()
            self.restore_home(root, previous)

    def test_mcp_runtime_protocol_edge_normalization_and_discovery_failure_state(self):
        root, home, workspace, previous = self.temp_context()
        mode = {"resources": "ok"}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                method = request.get("method")
                if method == "resources/list" and mode["resources"] == "fail":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"resource listing failed")
                    return
                result = {}
                if method == "initialize":
                    result = {"serverInfo": {"name": "edge"}}
                elif method == "tools/list":
                    result = {
                        "tools": [
                            {"name": "zero", "input_schema": {"type": "object", "properties": {"value": {"type": "number"}}}},
                            {"name": "empty", "inputSchema": {"type": "object"}},
                        ]
                    }
                elif method == "tools/call":
                    result_value = None if request.get("params", {}).get("name") == "empty" else 0
                    payload = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result_value}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                elif method == "resources/list":
                    result = {"resources": [{"uri": "memory://named", "name": "named"}, {"uri": "memory://missing-name"}]}
                elif method == "resources/read":
                    result = {"contents": [{"uri": "memory://named", "mimeType": None, "text": "named text"}]}
                payload = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = None
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/rpc"
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [{"name": "edge", "transport": "http", "commandOrUrl": url, "updatedAt": "now"}]}))
            tool = runtime["listTools"]()[0]
            self.assertEqual(tool["inputSchema"]["properties"]["value"]["type"], "number")
            self.assertEqual(run(runtime["callTool"]("edge", "zero", {"value": 0}))["content"], "0")
            self.assertEqual(run(runtime["callTool"]("edge", "empty", {}))["content"], "null")
            self.assertEqual([resource["uri"] for resource in run(runtime["listResources"]("edge"))], ["memory://named"])
            resource_payload = run(runtime["readResource"]("edge", "memory://named"))["contents"][0]
            self.assertEqual(resource_payload, {"uri": "memory://named", "text": "named text"})
            mode["resources"] = "fail"
            with self.assertRaises(Exception):
                run(runtime["listResources"]("edge"))
            status = runtime["listServerStatus"]()[0]
            self.assertFalse(status["connected"])
            self.assertEqual(status["state"], "degraded")
            self.assertIn("HTTP 500 from MCP server edge", status["error"])
        finally:
            if runtime:
                run(runtime["close"]())
            server.shutdown()
            server.server_close()
            self.restore_home(root, previous)

    def test_mcp_runtime_generated_tools_honor_abort_before_and_after_call(self):
        root, home, workspace, previous = self.temp_context()
        calls = {"tool": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                method = request.get("method")
                result = {}
                if method == "initialize":
                    result = {"serverInfo": {"name": "generated"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "run", "inputSchema": {"type": "object"}}]}
                elif method == "resources/list":
                    result = {"resources": []}
                elif method == "tools/call":
                    calls["tool"] += 1
                    result = {"content": [{"type": "text", "text": "ran"}]}
                raw = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class FlipSignal:
            def __init__(self):
                self.count = 0

            @property
            def aborted(self):
                self.count += 1
                return self.count >= 2

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = None
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/rpc"
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [{"name": "generated", "transport": "http", "commandOrUrl": url, "updatedAt": "now"}]}))
            generated_tool = runtime["createToolDefinitions"]()[0]
            # bypassPermissions: this test exercises abort semantics, not the
            # mcp__* permission gate (covered in test_mcp_permission_gate.py).
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            base_context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}

            with self.assertRaises(RuntimeError) as pre_ctx:
                run(generated_tool.handler({}, {**base_context, "abortSignal": {"aborted": True}}))
            self.assertEqual(getattr(pre_ctx.exception, "name", None), "AbortError")
            self.assertEqual(calls["tool"], 0)

            with self.assertRaises(RuntimeError) as post_ctx:
                run(generated_tool.handler({}, {**base_context, "abortSignal": FlipSignal()}))
            self.assertEqual(getattr(post_ctx.exception, "name", None), "AbortError")
            self.assertEqual(calls["tool"], 1)
        finally:
            if runtime:
                run(runtime["close"]())
            server.shutdown()
            server.server_close()
            self.restore_home(root, previous)

    def test_mcp_protocol_rejects_malformed_ids_batches_and_invalid_json(self):
        root, home, workspace, previous = self.temp_context()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                method = request.get("method")
                request_id = request.get("id")
                if method == "initialize":
                    payload = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "serverInfo": {"name": "protocol-edge"},
                            "capabilities": {"tools": {"listChanged": True}, "resources": {"listChanged": True}},
                        },
                    }
                elif method == "tools/list":
                    payload = {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "edge", "inputSchema": {"type": "object"}}]}}
                elif method == "resources/list":
                    payload = {"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}}
                elif method == "tools/call":
                    variant = request.get("params", {}).get("arguments", {}).get("variant")
                    if variant == "batch":
                        self._send_raw(json.dumps([{"jsonrpc": "2.0", "id": request_id, "result": {}}]).encode())
                        return
                    if variant == "missing":
                        self._send_raw(json.dumps({"jsonrpc": "2.0", "result": {}}).encode())
                        return
                    if variant == "wrong":
                        self._send_raw(json.dumps({"jsonrpc": "2.0", "id": int(request_id or 0) + 1, "result": {}}).encode())
                        return
                    if variant == "invalid":
                        self._send_raw(b"{not-json")
                        return
                    payload = {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": "ok"}]}}
                else:
                    payload = {"jsonrpc": "2.0", "id": request_id, "result": {}}
                self._send_raw(json.dumps(payload).encode())

            def _send_raw(self, raw):
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = None
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/rpc"
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [{"name": "edge", "transport": "http", "commandOrUrl": url, "updatedAt": "now"}]}))
            status = runtime["listServerStatus"]()[0]
            self.assertEqual(status["serverCapabilities"], {"tools": {"listChanged": True}, "resources": {"listChanged": True}})
            for variant, message in [
                ("batch", "batch JSON-RPC response"),
                ("missing", "missing JSON-RPC id"),
                ("wrong", "response id did not match"),
                ("invalid", "Invalid JSON-RPC response"),
            ]:
                with self.subTest(variant=variant):
                    with self.assertRaisesRegex(RuntimeError, message):
                        run(runtime["callTool"]("edge", "edge", {"variant": variant}))
                    self.assertEqual(runtime["listServerStatus"]()[0]["state"], "degraded")
            self.assertEqual(run(runtime["callTool"]("edge", "edge", {"variant": "ok"}))["content"], "ok")
        finally:
            if runtime:
                run(runtime["close"]())
            server.shutdown()
            server.server_close()
            self.restore_home(root, previous)

    def test_mcp_http_sse_response_requires_matching_json_rpc_id(self):
        root, home, workspace, previous = self.temp_context()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                method = request.get("method")
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    payload = {"jsonrpc": "2.0", "id": request.get("id"), "result": {"serverInfo": {"name": "sse-edge"}}}
                    raw = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if method == "tools/list":
                    payload = {"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": [{"name": "run", "inputSchema": {"type": "object"}}]}}
                    raw = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if method == "resources/list":
                    payload = {"jsonrpc": "2.0", "id": request.get("id"), "result": {"resources": []}}
                    raw = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                wrong_id = int(request.get("id") or 0) + 100
                payload = {"jsonrpc": "2.0", "id": wrong_id, "result": {"content": [{"type": "text", "text": "wrong id"}]}}
                raw = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = None
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/rpc"
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [{"name": "sse-edge", "transport": "http", "commandOrUrl": url, "updatedAt": "now"}]}))
            with self.assertRaisesRegex(RuntimeError, "No matching SSE JSON-RPC response was received"):
                run(runtime["callTool"]("sse-edge", "run", {}))
            status = runtime["listServerStatus"]()[0]
            self.assertFalse(status["connected"])
            self.assertEqual(status["state"], "degraded")
            self.assertEqual(
                status["error"],
                "invalid_framing: No matching SSE JSON-RPC response was received.",
            )
        finally:
            if runtime:
                run(runtime["close"]())
            server.shutdown()
            server.server_close()
            self.restore_home(root, previous)

    def test_mcp_sse_endpoint_and_id_matching_follow_reference_edges(self):
        client = mcp_module.SseMcpClient({"name": "edge", "commandOrUrl": "http://127.0.0.1:1/sse"}, "/tmp")
        client._handle_sse_event("endpoint", [json.dumps({"url": "/messages"})])
        self.assertEqual(client.post_url, "http://127.0.0.1:1/messages")

        http_client = mcp_module.HttpMcpClient({"name": "edge", "transport": "http", "commandOrUrl": "http://127.0.0.1:1/mcp"}, "/tmp")
        no_id = http_client._consume_rpc_sse_event({"data": '{"jsonrpc":"2.0","result":{"ok":true}}'}, None)
        self.assertEqual(no_id["result"], {"ok": True})
        self.assertIsNone(http_client._consume_rpc_sse_event({"data": '{"jsonrpc":"2.0","id":2,"result":{}}'}, 1))
        for malformed in ('{"jsonrpc":"2.0","id":"1","result":{}}', '{"jsonrpc":"2.0","id":true,"result":{}}'):
            with self.assertRaises(mcp_module.McpTransportError):
                http_client._consume_rpc_sse_event({"data": malformed}, 1)

    def test_mcp_discovery_only_swallows_method_not_found(self):
        root, home, workspace, previous = self.temp_context()
        mode = {"tools": "method_not_found"}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                method = request.get("method")
                if method == "initialize":
                    payload = {"jsonrpc": "2.0", "id": request.get("id"), "result": {"serverInfo": {"name": "edge"}}}
                elif method == "tools/list" and mode["tools"] == "method_not_found":
                    payload = {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": "Method not found"}}
                elif method == "tools/list":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"discovery failed")
                    return
                elif method == "resources/list":
                    payload = {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": "Method not found"}}
                else:
                    payload = {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = None
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/rpc"
            events = []
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [{"name": "edge", "transport": "http", "commandOrUrl": url, "updatedAt": "now"}], "registeredChannels": ["edge"], "sessionId": "session-1", "onRuntimeEvent": lambda event, data: events.append((event, data))}))
            status = runtime["listServerStatus"]()[0]
            self.assertTrue(status["connected"])
            self.assertEqual(status["tools"], 0)
            self.assertTrue(status["capabilities"]["channelsSupported"])
            self.assertTrue(any(event == "mcp.channel_registered" and data["server"] == "edge" for event, data in events))
            run(runtime["close"]())
            mode["tools"] = "server_error"
            events = []
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [{"name": "edge", "transport": "http", "commandOrUrl": url, "updatedAt": "now"}], "onRuntimeEvent": lambda event, data: events.append((event, data))}))
            status = runtime["listServerStatus"]()[0]
            self.assertFalse(status["connected"])
            self.assertEqual(status["state"], "disconnected")
            self.assertTrue(any(event == "mcp.server_connection_failed" for event, _ in events))
        finally:
            if runtime:
                run(runtime["close"]())
            server.shutdown()
            server.server_close()
            self.restore_home(root, previous)

    def test_mcp_channel_registration_failure_events_and_capability_status(self):
        root, home, workspace, previous = self.temp_context()
        events = []
        try:
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [], "registeredChannels": ["missing"], "sessionId": "session-1", "onRuntimeEvent": lambda event, data: events.append((event, data))}))
            self.assertTrue(any(event == "mcp.channel_registration_failed" and data["server"] == "missing" for event, data in events))
            run(runtime["close"]())
        finally:
            self.restore_home(root, previous)
