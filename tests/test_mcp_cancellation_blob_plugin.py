from __future__ import annotations

from tests.support.agent_core_case import *


class McpCancellationBlobPluginTests(AgentCoreTestCase):
    def test_stdio_mcp_cancellation_cleans_pending_request_and_keeps_connection(self):
        root, home, workspace, previous = self.temp_context()
        try:
            server = workspace / "slow_mcp.py"
            server.write_text(
                textwrap.dedent(
                    r'''
                    import json
                    import sys
                    import time

                    calls = 0

                    def read_message():
                        header = b""
                        while not header.endswith(b"\r\n\r\n"):
                            chunk = sys.stdin.buffer.read(1)
                            if not chunk:
                                return None
                            header += chunk
                        length = 0
                        for line in header.decode().splitlines():
                            if line.lower().startswith("content-length:"):
                                length = int(line.split(":", 1)[1].strip())
                        return json.loads(sys.stdin.buffer.read(length).decode())

                    def send(message):
                        payload = json.dumps(message).encode()
                        sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
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
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"serverInfo": {"name": "slow"}}})
                        elif method == "tools/list":
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "slow", "inputSchema": {"type": "object"}}]}})
                        elif method == "resources/list":
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}})
                        elif method == "tools/call":
                            calls += 1
                            if calls == 1:
                                time.sleep(0.3)
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": f"call-{calls}"}]}})
                        else:
                            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                    '''
                ),
                encoding="utf-8",
            )
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [{"name": "slow", "transport": "stdio", "commandOrUrl": sys.executable, "args": [str(server)], "updatedAt": "now", "requestTimeoutSeconds": 2}]}))

            async def cancel_then_retry():
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(runtime["callTool"]("slow", "slow", {}), timeout=0.05)
                result = await runtime["callTool"]("slow", "slow", {})
                return result

            result = run(cancel_then_retry())
            self.assertEqual(result["content"], "call-2")
            self.assertTrue(runtime["listServerStatus"]()[0]["connected"])
            run(runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_mcp_blob_lifecycle_uses_stable_paths_and_cleanup(self):
        root, home, workspace, previous = self.temp_context()
        previous_keep = os.environ.get("AGENT_CORE_MCP_BLOB_KEEP")
        os.environ["AGENT_CORE_MCP_BLOB_KEEP"] = "2"
        try:
            first = base64.b64encode(b"a" * 65536).decode("ascii")
            second = base64.b64encode(b"b" * 1024).decode("ascii")
            third = base64.b64encode(b"c" * 1024).decode("ascii")
            first_path = run(mcp_module.normalization._normalize_read_resource_output("blob", {"contents": [{"uri": "memory://bundle/large.bin", "mimeType": "application/octet-stream", "blob": first}]}))["contents"][0]["blobSavedTo"]
            repeated_path = run(mcp_module.normalization._normalize_read_resource_output("blob", {"contents": [{"uri": "memory://bundle/large.bin", "mimeType": "application/octet-stream", "data": first}]}))["contents"][0]["blobSavedTo"]
            self.assertEqual(first_path, repeated_path)
            self.assertEqual(Path(first_path).read_bytes(), b"a" * 65536)
            second_path = run(mcp_module.normalization._normalize_read_resource_output("blob", {"contents": [{"uri": "memory://bundle/second.bin", "blob": second}]}))["contents"][0]["blobSavedTo"]
            third_path = run(mcp_module.normalization._normalize_read_resource_output("blob", {"contents": [{"uri": "memory://bundle/third.bin", "blob": third}]}))["contents"][0]["blobSavedTo"]
            self.assertTrue(Path(second_path).exists())
            self.assertTrue(Path(third_path).exists())
            self.assertFalse(Path(first_path).exists())
        finally:
            if previous_keep is None:
                os.environ.pop("AGENT_CORE_MCP_BLOB_KEEP", None)
            else:
                os.environ["AGENT_CORE_MCP_BLOB_KEEP"] = previous_keep
            self.restore_home(root, previous)

    def test_plugin_mcp_server_conflict_uses_last_plugin_server_definition(self):
        root, home, workspace, previous = self.temp_context()
        server = None
        boot = None
        try:
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    return

                def do_POST(self):
                    length = int(self.headers.get("content-length") or 0)
                    request = json.loads(self.rfile.read(length).decode("utf-8"))
                    method = request.get("method")
                    result = {}
                    if method == "initialize":
                        result = {"serverInfo": {"name": "plugin-shared"}}
                    elif method == "tools/list":
                        result = {"tools": [{"name": "plugin_tool", "description": "Plugin tool", "inputSchema": {"type": "object"}}]}
                    elif method == "resources/list":
                        result = {"resources": []}
                    payload = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            plugin_dir = workspace / "mcp-conflict-plugin"
            plugin_dir.mkdir()
            (plugin_dir / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "conflict",
                        "version": "1.0.0",
                        "mcpServers": [
                            {
                                "name": "shared",
                                "transport": "http",
                                "commandOrUrl": f"http://127.0.0.1:{server.server_address[1]}/rpc",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default", "pluginDir": [str(plugin_dir)]}, str(workspace)))
            config["mcpConfig"] = [{"name": "shared", "transport": "http", "commandOrUrl": "http://127.0.0.1:1/broken", "updatedAt": "now"}]
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            boot = run(core.bootstrapRuntime({"session": session, "config": config}))
            tools = boot["mcpRuntime"]["listTools"]()
            self.assertEqual([tool["runtimeName"] for tool in tools], ["mcp__shared__plugin_tool"])
            self.assertEqual(boot["mcpRuntime"]["listServerStatus"]()[0]["serverInfo"]["name"], "plugin-shared")
        finally:
            if boot:
                run(boot["close"]())
            if server:
                server.shutdown()
                server.server_close()
            self.restore_home(root, previous)
