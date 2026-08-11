from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.mcp.normalization import _normalize_tool_call_result


class McpRichContentTests(AgentCoreTestCase):
    def test_image_audio_and_blob_resources_spill_and_attach(self):
        root, home, workspace, previous = self.temp_context()
        try:
            png = b"\x89PNG-fake-image-bytes"
            wav = b"RIFF-fake-audio-bytes"
            doc = b"embedded-blob-bytes"
            result = run(
                _normalize_tool_call_result(
                    "srv",
                    "shot",
                    {
                        "content": [
                            {"type": "text", "text": "caption"},
                            {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()},
                            {"type": "audio", "mimeType": "audio/wav", "data": base64.b64encode(wav).decode()},
                            {
                                "type": "resource",
                                "resource": {
                                    "uri": "mem://doc.bin",
                                    "mimeType": "application/octet-stream",
                                    "blob": base64.b64encode(doc).decode(),
                                },
                            },
                            {"type": "resource", "resource": {"uri": "mem://note.txt", "text": "inline note"}},
                        ],
                    },
                )
            )
            content = result["content"]
            self.assertIn("caption", content)
            self.assertIn("inline note", content)
            # Binary payloads spill to disk instead of being dumped as base64.
            self.assertNotIn(base64.b64encode(png).decode(), content)
            self.assertNotIn(base64.b64encode(wav).decode(), content)
            saved_paths = re.findall(r'"savedTo": "([^"]+)"', content)
            self.assertEqual(len(saved_paths), 3)
            for path in saved_paths:
                self.assertTrue(Path(path).exists(), path)
            attachments = result["attachments"]
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0]["mimeType"], "image/png")
            self.assertEqual(base64.b64decode(attachments[0]["data"]), png)
            self.assertEqual(attachments[0]["byteLength"], len(png))
        finally:
            self.restore_home(root, previous)

    def test_invalid_base64_binary_block_degrades_to_error_text(self):
        root, home, workspace, previous = self.temp_context()
        try:
            result = run(
                _normalize_tool_call_result(
                    "srv",
                    "bad",
                    {"content": [{"type": "image", "mimeType": "image/png", "data": "!!!not-base64!!!"}]},
                )
            )
            self.assertIn("invalid base64", result["content"])
            self.assertNotIn("attachments", result)
            self.assertFalse(result["isError"])
        finally:
            self.restore_home(root, previous)


class BootstrapRefreshContractTests(AgentCoreTestCase):
    def test_bootstrap_refresh_returns_live_registry_even_when_unchanged(self):
        # Regression: the hook must hand back the LIVE tool list on every
        # call. Returning None on "no new change" made each later turn revert
        # to the startup snapshot captured in the agent bindings, so a catalog
        # change survived for exactly one turn.
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"runtimeHome": str(home)}, str(workspace)))
            session = core.create_empty_session(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )
            boot = run(core.bootstrapRuntime(session=session, config=config, daemonEnabled=False))
            try:
                first = run(boot["refreshMcpToolRegistry"]())
                second = run(boot["refreshMcpToolRegistry"]())
                for refreshed in (first, second):
                    self.assertIsInstance(refreshed, list)
                    self.assertTrue(refreshed)
                self.assertEqual(
                    [tool.name for tool in second],
                    [tool.name for tool in boot["tools"]],
                )
            finally:
                run(boot["close"]())
        finally:
            self.restore_home(root, previous)


class McpRefreshLastHopTests(AgentCoreTestCase):
    def _config(self, cwd: str) -> dict:
        return {
            "cwd": cwd,
            "runtimeHome": cwd,
            "allowedRoots": [cwd],
            "agents": {},
            "provider": "anthropic",
            "providerKind": "anthropic",
            "providerProfileName": "default",
            "providerProfile": {"kind": "anthropic", "name": "default", "auth": {}},
            "providerProfiles": {"default": {"kind": "anthropic", "name": "default"}},
            "customHeaders": {},
            "openaiApiMode": "responses",
            "model": "claude-sonnet-4-6",
            "effort": "medium",
            "assistantName": "Test",
            "assistantDescription": "",
            "systemPrompt": "test",
            "tools": ["fresh"],
            "allowedTools": ["fresh"],
            "disallowedTools": [],
            "permissionMode": "bypassPermissions",
            "sessionPersistence": False,
        }

    def test_turn_adopts_refreshed_tool_registry(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-mcp-refresh-") as cwd:
            config = self._config(cwd)
            session = core.create_empty_session(cwd=cwd, provider="anthropic", model="claude-sonnet-4-6", effort="medium", systemPrompt="test", permissionMode="bypassPermissions")

            async def stale_handler(_input, _context):
                return {"content": "STALE", "isError": False}

            async def fresh_handler(_input, _context):
                return {"content": "FRESH", "isError": False}

            stale_tool = core.ToolDefinition("stale", "old registry", {"type": "object"}, stale_handler)
            fresh_tool = core.ToolDefinition("fresh", "refreshed registry", {"type": "object"}, fresh_handler)

            async def refresh():
                # The bootstrap contract: the recomposed tool list when the MCP
                # catalog changed, None otherwise.
                return [fresh_tool]

            calls = {"count": 0}

            async def send(_request):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "model": "claude-sonnet-4-6",
                        "stopReason": "tool_use",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "content": [{"type": "tool_use", "id": "use_fresh", "name": "fresh", "input": {}}],
                    }
                return {
                    "model": "claude-sonnet-4-6",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                    "content": [{"type": "text", "text": "done"}],
                }

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(
                        session,
                        {
                            "interactive": False,
                            "askQuestion": lambda *_: "",
                            "tools": [stale_tool],
                            "refreshMcpToolRegistry": refresh,
                        },
                    ),
                    prompt="run",
                    providerOverride={"send": send},
                )
            )
            self.assertEqual(result["toolCount"], 1)
            tool_result_message = next(
                message
                for message in session["messages"]
                if message["role"] == "user" and isinstance(message["content"], list)
            )
            blocks = [block for block in tool_result_message["content"] if block["type"] == "tool_result"]
            self.assertEqual(blocks[0]["content"], "FRESH")
