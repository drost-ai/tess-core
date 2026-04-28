from __future__ import annotations

from tests.support.agent_core_case import *


class AgentLoopTests(AgentCoreTestCase):
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

            async def send(_request):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {"model": "claude-sonnet-4-6", "stopReason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": [{"type": "tool_use", "id": "use_slow", "name": "slow", "input": {}}, {"type": "tool_use", "id": "use_fast", "name": "fast", "input": {}}]}
                return {"model": "claude-sonnet-4-6", "stopReason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 3}, "content": [{"type": "text", "text": "done"}]}

            result = run(core.runAgentPrompt(session=session, config=config, runtime={"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda *_: None, "tools": tools}, prompt="run", providerOverride={"send": send}))
            self.assertEqual(result["toolCount"], 2)
            starts = sorted(item[2] for item in log if item[1] == "start")
            ends = sorted(item[2] for item in log if item[1] == "end")
            self.assertLess(starts[1], ends[0])
            tool_result_message = next(message for message in session["messages"] if message["role"] == "user" and isinstance(message["content"], list))
            blocks = [block for block in tool_result_message["content"] if block["type"] == "tool_result"]
            self.assertEqual(blocks[0]["tool_use_id"], "use_slow")
            self.assertEqual(blocks[0]["content"], "SLOW")
            self.assertEqual(blocks[1]["tool_use_id"], "use_fast")
            self.assertEqual(blocks[1]["content"], "FAST")

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
