from __future__ import annotations

from tests.support.agent_core_case import *


class AgentLoopTests(AgentCoreTestCase):
    def test_run_agent_prompt_rejects_image_inputs_for_non_vision_profile(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-vision-") as cwd:
            config = {
                "cwd": cwd,
                "runtimeHome": cwd,
                "allowedRoots": [cwd],
                "agents": {},
                "provider": "openai",
                "providerKind": "openai",
                "providerProfileName": "deepseek",
                "providerProfile": {
                    "kind": "openai",
                    "name": "deepseek",
                    "baseUrl": "https://api.deepseek.com",
                    "supportsVision": False,
                },
                "providerProfiles": {"deepseek": {"kind": "openai", "name": "deepseek"}},
                "customHeaders": {},
                "openaiApiMode": "chat_completions",
                "openaiBaseUrl": "https://api.deepseek.com",
                "model": "deepseek-v4-pro",
                "effort": "medium",
                "assistantName": "Test",
                "assistantDescription": "",
                "systemPrompt": "test",
                "tools": [],
                "allowedTools": [],
                "disallowedTools": [],
                "permissionMode": "bypassPermissions",
                "sessionPersistence": False,
            }
            session = core.createEmptySession(
                cwd=cwd,
                provider="openai",
                model="deepseek-v4-pro",
                effort="medium",
                systemPrompt="test",
                permissionMode="bypassPermissions",
            )
            session["messages"].append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image", "mime_type": "image/png", "data": base64.b64encode(b"img").decode()},
                    ],
                }
            )

            async def send(_request):
                raise AssertionError("provider should not be called for unsupported image input")

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime={"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda *_: None, "tools": []},
                    prompt="",
                    providerOverride={"send": send},
                )
            )

            self.assertEqual(result["text"], core.IMAGE_INPUT_UNSUPPORTED_MESSAGE)
            self.assertEqual(result["stopReason"], "unsupported_image_input")
            self.assertEqual(session["messages"][-1]["content"][0]["text"], core.IMAGE_INPUT_UNSUPPORTED_MESSAGE)

    def test_run_agent_prompt_sends_image_inputs_for_vision_profile(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-vision-") as cwd:
            config = {
                "cwd": cwd,
                "runtimeHome": cwd,
                "allowedRoots": [cwd],
                "agents": {},
                "provider": "openai",
                "providerKind": "openai",
                "providerProfileName": "local-vision",
                "providerProfile": {
                    "kind": "openai",
                    "name": "local-vision",
                    "baseUrl": "http://127.0.0.1:8080/v1",
                    "supportsVision": True,
                },
                "providerProfiles": {"local-vision": {"kind": "openai", "name": "local-vision"}},
                "customHeaders": {},
                "openaiApiMode": "chat_completions",
                "openaiBaseUrl": "http://127.0.0.1:8080/v1",
                "model": "local-model",
                "effort": "medium",
                "assistantName": "Test",
                "assistantDescription": "",
                "systemPrompt": "test",
                "tools": [],
                "allowedTools": [],
                "disallowedTools": [],
                "permissionMode": "bypassPermissions",
                "sessionPersistence": False,
            }
            session = core.createEmptySession(
                cwd=cwd,
                provider="openai",
                model="local-model",
                effort="medium",
                systemPrompt="test",
                permissionMode="bypassPermissions",
            )
            session["messages"].append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image", "mime_type": "image/png", "data": "aW1n"},
                    ],
                }
            )
            captured = {}

            async def send(request):
                captured["request"] = request
                return {
                    "model": "local-model",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                    "content": [{"type": "text", "text": "vision ok"}],
                }

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime={"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda *_: None, "tools": []},
                    prompt="",
                    providerOverride={"send": send},
                )
            )

            self.assertEqual(result["text"], "vision ok")
            content = captured["request"]["messages"][0]["content"]
            self.assertEqual(content[0], {"type": "text", "text": "What is this?"})
            self.assertEqual(content[1]["type"], "image")

    def test_run_agent_prompt_concurrent_tool_dispatch_and_order(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-concurrent-") as cwd:
            config = {
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
                "tools": ["slow", "fast"],
                "allowedTools": ["slow", "fast"],
                "disallowedTools": [],
                "permissionMode": "bypassPermissions",
                "sessionPersistence": False,
            }
            session = core.createEmptySession(cwd=cwd, provider="anthropic", model="claude-sonnet-4-6", effort="medium", systemPrompt="test", permissionMode="bypassPermissions")
            log = []

            async def slow_handler(_input, _context):
                log.append(("slow", "start", time.time()))
                await asyncio.sleep(0.12)
                log.append(("slow", "end", time.time()))
                return {"content": "SLOW", "isError": False}

            async def fast_handler(_input, _context):
                log.append(("fast", "start", time.time()))
                await asyncio.sleep(0.01)
                log.append(("fast", "end", time.time()))
                return {"content": "FAST", "isError": False}

            tools = [
                core.ToolDefinition("slow", "slow tool", {"type": "object"}, slow_handler),
                core.ToolDefinition("fast", "fast tool", {"type": "object"}, fast_handler),
            ]
            calls = {"count": 0}
            events = []

            async def send(_request):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {"model": "claude-sonnet-4-6", "stopReason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": [{"type": "tool_use", "id": "use_slow", "name": "slow", "input": {}}, {"type": "tool_use", "id": "use_fast", "name": "fast", "input": {}}]}
                return {"model": "claude-sonnet-4-6", "stopReason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 3}, "content": [{"type": "text", "text": "done"}]}

            result = run(core.runAgentPrompt(session=session, config=config, runtime={"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda event, data: events.append((event, data)), "tools": tools}, prompt="run", providerOverride={"send": send}))
            self.assertEqual(result["toolCount"], 2)
            provider_events = [data for event, data in events if event == "provider.response_completed"]
            self.assertEqual([item["call_index"] for item in provider_events], [1, 2])
            self.assertEqual(provider_events[0]["usage"]["input_tokens"], 10)
            self.assertEqual(provider_events[1]["usage"]["output_tokens"], 3)
            starts = sorted(item[2] for item in log if item[1] == "start")
            ends = sorted(item[2] for item in log if item[1] == "end")
            self.assertLess(starts[1], ends[0])
            tool_result_message = next(message for message in session["messages"] if message["role"] == "user" and isinstance(message["content"], list))
            blocks = [block for block in tool_result_message["content"] if block["type"] == "tool_result"]
            self.assertEqual(blocks[0]["tool_use_id"], "use_slow")
            self.assertEqual(blocks[0]["content"], "SLOW")
            self.assertEqual(blocks[1]["tool_use_id"], "use_fast")
            self.assertEqual(blocks[1]["content"], "FAST")

    def test_run_agent_prompt_turns_invalid_streamed_tool_input_into_repairable_tool_result(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-invalid-tool-") as cwd:
            config = {
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
                "tools": ["Edit"],
                "allowedTools": ["Edit"],
                "disallowedTools": [],
                "permissionMode": "bypassPermissions",
                "sessionPersistence": False,
            }
            session = core.createEmptySession(cwd=cwd, provider="anthropic", model="claude-sonnet-4-6", effort="medium", systemPrompt="test", permissionMode="bypassPermissions")
            tool_ran = False
            calls = {"count": 0}

            async def edit_handler(_input, _context):
                nonlocal tool_ran
                tool_ran = True
                return {"content": "edited", "isError": False}

            tools = [core.ToolDefinition("Edit", "edit file", {"type": "object"}, edit_handler)]

            async def send(request):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "model": "claude-sonnet-4-6",
                        "stopReason": "tool_use",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "bad_edit",
                                "name": "Edit",
                                "input": {},
                                "_agent_core_invalid_tool_input": True,
                                "_agent_core_invalid_tool_input_error": "The provider streamed incomplete or invalid JSON for this tool call.",
                            }
                        ],
                    }
                self.assertTrue(any(block.get("is_error") for message in request["messages"] if isinstance(message.get("content"), list) for block in message["content"] if block.get("type") == "tool_result"))
                return {
                    "model": "claude-sonnet-4-6",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                    "content": [{"type": "text", "text": "retrying with valid JSON next"}],
                }

            result = run(core.runAgentPrompt(session=session, config=config, runtime={"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda *_: None, "tools": tools}, prompt="run", providerOverride={"send": send}))
            self.assertFalse(tool_ran)
            self.assertEqual(calls["count"], 2)
            self.assertEqual(result["text"], "retrying with valid JSON next")
            tool_result_message = next(message for message in session["messages"] if message["role"] == "user" and isinstance(message["content"], list))
            self.assertTrue(tool_result_message["content"][0]["is_error"])
            self.assertIn("invalid JSON", tool_result_message["content"][0]["content"])

    def test_run_agent_prompt_cancels_turn_and_restores_session(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-cancel-") as cwd:
            config = {
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
                "tools": [],
                "allowedTools": [],
                "disallowedTools": [],
                "permissionMode": "bypassPermissions",
                "sessionPersistence": False,
            }
            session = core.createEmptySession(cwd=cwd, provider="anthropic", model="claude-sonnet-4-6", effort="medium", systemPrompt="test", permissionMode="bypassPermissions")
            signal = SimpleNamespace(aborted=False, reason=None)
            events = []

            async def send(_request):
                signal.aborted = True
                error = RuntimeError("provider aborted")
                error.name = "AbortError"
                raise error

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime={"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda event, data: events.append((event, data)), "tools": [], "abortSignal": signal},
                    prompt="cancel this turn",
                    providerOverride={"send": send},
                )
            )
            self.assertEqual(result["stopReason"], "cancelled")
            self.assertEqual(session["messages"], [])
            self.assertTrue(any(event == "turn.cancelled" and data["session_id"] == session["id"] for event, data in events))
