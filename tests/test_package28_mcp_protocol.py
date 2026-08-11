from __future__ import annotations

from tests.support.agent_core_case import *


class Package28McpProtocolTests(AgentCoreTestCase):
    def test_protocol_negotiation_current_intermediate_legacy_missing_and_unsupported(self):
        async def initialize(version_marker):
            class Client(mcp_module.JsonRpcClient):
                async def _send(self, request):
                    if request["method"] == "initialize":
                        result = {"serverInfo": {"name": "fake"}, "capabilities": {"tools": {"listChanged": True}}}
                        if version_marker is not None:
                            result["protocolVersion"] = version_marker
                        return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                    return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}

            client = Client({"name": "fake"}, "/tmp")
            await client.initialize()
            return client

        for version in ("2025-06-18", "2025-03-26", "2024-11-05"):
            with self.subTest(version=version):
                client = run(initialize(version))
                self.assertEqual(client.negotiated_protocol_version, version)
                self.assertTrue(client.server_capabilities["tools"]["listChanged"])
        self.assertEqual(run(initialize(None)).negotiated_protocol_version, "2024-11-05")
        with self.assertRaisesRegex(RuntimeError, "unsupported protocol version 2099-01-01"):
            run(initialize("2099-01-01"))

    def test_http_requests_send_the_negotiated_protocol_header(self):
        captured = []

        class Headers:
            def get(self, key, default=None):
                return "application/json" if key.lower() == "content-type" else default

        class Response:
            status = 200
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"jsonrpc":"2.0","id":1,"result":{}}'

        def fake_urlopen(request, timeout=None):
            captured.append((request, timeout))
            return Response()

        client = mcp_module.HttpMcpClient(
            {"name": "fake", "commandOrUrl": "https://example.test/mcp", "headers": {"x-static": "yes"}},
            "/tmp",
        )
        client.negotiated_protocol_version = "2025-06-18"
        with mock.patch.object(mcp_module.http_body, "urlopen", fake_urlopen):
            run(client.request("tools/list"))
        request = captured[0][0]
        self.assertEqual(request.get_header("Mcp-protocol-version"), "2025-06-18")
        self.assertEqual(request.get_header("X-static"), "yes")

    def test_structured_content_is_preserved_bounded_and_model_visible(self):
        huge = {f"key-{index}": "x" * 4_000 for index in range(100)}
        result = run(
            mcp_module.normalization._normalize_tool_call_result(
                "fake",
                "structured",
                {
                    "content": [{"type": "text", "text": "summary"}],
                    "structuredContent": huge,
                },
            )
        )
        self.assertIn("structuredContent", result)
        self.assertLessEqual(len(result["structuredContent"]), 40)
        self.assertLess(len(json.dumps(result["structuredContent"]).encode("utf-8")), 40_000)
        model_payload = json.loads(result["content"])
        self.assertEqual(model_payload["text"], "summary")
        self.assertEqual(model_payload["structured"], result["structuredContent"])

    def test_catalog_refresh_is_atomic_at_boundary_and_keeps_known_good_on_failure(self):
        async def exercise():
            captured_state = None
            events = []

            class Client:
                def __init__(self):
                    self.next_tools = [
                        {
                            "name": "old",
                            "inputSchema": {"type": "object"},
                            "annotations": {"readOnlyHint": True},
                        }
                    ]
                    self.fail = False

                async def request(self, method, _params=None):
                    if self.fail:
                        raise RuntimeError("candidate failed")
                    if method == "tools/list":
                        return {"tools": self.next_tools}
                    if method == "resources/list":
                        return {"resources": []}
                    return {}

                async def close(self):
                    return None

            client = Client()

            async def connect(state, _cwd, _on_event):
                nonlocal captured_state
                captured_state = state
                state.update(
                    {
                        "client": client,
                        "connected": True,
                        "state": "connected",
                        "tools": [
                            {
                                "server": "fake",
                                "name": "old",
                                "runtimeName": "mcp__fake__old",
                                "inputSchema": {"type": "object"},
                                "annotations": {"readOnlyHint": True},
                            }
                        ],
                        "resources": [],
                        "catalogGeneration": 1,
                    }
                )

            with mock.patch.object(mcp_module.runtime, "_connect_state", connect):
                runtime = await core.createMcpRuntime(
                    cwd="/tmp",
                    servers=[{"name": "fake", "transport": "http", "commandOrUrl": "https://example.test/mcp"}],
                    onRuntimeEvent=lambda event, data: events.append((event, data)),
                )

            active_turn_definitions = runtime["createToolDefinitions"]()
            client.next_tools = [
                {"name": "new", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": False}}
            ]
            mcp_module.state._emit_mcp_notification(
                captured_state["config"],
                {"method": "notifications/tools/list_changed", "params": {"generation": 2}},
                lambda event, data: events.append((event, data)),
                captured_state,
            )
            self.assertEqual([tool["name"] for tool in runtime["listTools"]()], ["old"])
            self.assertEqual([tool.name for tool in active_turn_definitions], ["mcp__fake__old"])
            applied = await runtime["applyPendingCatalogChanges"]()
            self.assertTrue(applied["changed"])
            self.assertEqual(applied["generation"], 2)
            self.assertEqual([tool["name"] for tool in runtime["listTools"]()], ["new"])
            self.assertEqual([tool.name for tool in active_turn_definitions], ["mcp__fake__old"])
            self.assertEqual(runtime["createToolDefinitions"]()[0].concurrency, "exclusive")

            client.fail = True
            mcp_module.state._emit_mcp_notification(
                captured_state["config"],
                {"method": "notifications/tools/list_changed", "params": {}},
                lambda event, data: events.append((event, data)),
                captured_state,
            )
            failed = await runtime["applyPendingCatalogChanges"]()
            self.assertFalse(failed["changed"])
            self.assertEqual([tool["name"] for tool in runtime["listTools"]()], ["new"])
            self.assertTrue(captured_state["toolsStale"])
            self.assertEqual(captured_state["state"], "degraded")
            self.assertTrue(any(event == "mcp.catalog_refresh_failed" for event, _data in events))
            await runtime["close"]()

        run(exercise())

    def test_bootstrap_rebuilds_generated_tools_only_when_safe_boundary_is_applied(self):
        async def exercise():
            current = [core.ToolDefinition("mcp__fake__old", "old", {"type": "object"}, _protocol_ok)]

            async def apply_changes():
                current[:] = [core.ToolDefinition("mcp__fake__new", "new", {"type": "object"}, _protocol_ok)]
                return {"changed": True, "servers": ["fake"], "generation": 2}

            mcp_runtime = {
                "createToolDefinitions": lambda: list(current),
                "applyPendingCatalogChanges": apply_changes,
                "close": lambda: None,
            }
            with tempfile.TemporaryDirectory(prefix="agent-core-p28-catalog-bootstrap-") as cwd:
                config = await core.resolveConfig(
                    {"provider": "openai", "runtimeHome": cwd, "permissionMode": "bypassPermissions"},
                    cwd,
                )
                runtime = await core.bootstrap_runtime(cwd=cwd, config=config, mcpRuntime=mcp_runtime, daemonEnabled=False)
                self.assertIn("mcp__fake__old", [tool.name for tool in runtime["tools"]])
                self.assertNotIn("mcp__fake__new", [tool.name for tool in runtime["tools"]])
                applied = await runtime["applyPendingMcpCatalogChanges"]()
                self.assertEqual(applied["generation"], 2)
                self.assertNotIn("mcp__fake__old", [tool.name for tool in runtime["tools"]])
                self.assertIn("mcp__fake__new", [tool.name for tool in runtime["tools"]])
                await runtime["close"]()

        run(exercise())


async def _protocol_ok(_input, _context):
    return {"content": "ok"}
