from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import agent_core as core
from agent_core import testing
import agent_core.tools as tools_module


def run(coro):
    return asyncio.run(coro)


class AsyncChunks:
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.ok = 200 <= status < 300

    async def text(self):
        return self.body


class AgentCoreTests(unittest.TestCase):
    def temp_context(self):
        root = tempfile.TemporaryDirectory(prefix="agent-core-python-")
        home = Path(root.name) / "home"
        workspace = Path(root.name) / "workspace"
        home.mkdir()
        workspace.mkdir()
        previous = os.environ.get("AGENT_CORE_HOME")
        os.environ["AGENT_CORE_HOME"] = str(home)
        return root, home, workspace, previous

    def restore_home(self, root, previous):
        if previous is None:
            os.environ.pop("AGENT_CORE_HOME", None)
        else:
            os.environ["AGENT_CORE_HOME"] = previous
        root.cleanup()

    def test_public_entrypoints(self):
        self.assertTrue(callable(core.resolveConfig))
        self.assertTrue(callable(core.runAgentPrompt))
        self.assertTrue(callable(core.bootstrapRuntime))
        self.assertTrue(callable(core.createEmptySession))
        self.assertTrue(callable(core.ensureTaskSupervisorDaemon))

    def test_built_in_provider_profiles(self):
        profiles = core.resolveProviderProfiles(None)
        self.assertEqual(core.listProviderProfiles(profiles), ["anthropic-claude-code", "anthropic-console", "codex", "openai"])
        self.assertEqual(profiles["codex"]["defaultModel"], "gpt-5.4")

    def test_resolve_config_custom_profile(self):
        root, home, workspace, previous = self.temp_context()
        prev_key = os.environ.get("DEEPINFRA_API_KEY")
        os.environ["DEEPINFRA_API_KEY"] = "deepinfra-test-key"
        try:
            (home / "settings.json").write_text(
                json.dumps(
                    {
                        "provider": "deepinfra",
                        "providerProfiles": {
                            "deepinfra": {
                                "kind": "openai",
                                "apiMode": "chat_completions",
                                "baseUrl": "https://api.deepinfra.com/v1/openai",
                                "defaultModel": "zai-org/GLM-5",
                                "auth": {"kind": "custom_bearer", "envVar": "DEEPINFRA_API_KEY"},
                            }
                        },
                    }
                )
            )
            config = run(core.resolveConfig({}, str(workspace)))
            self.assertEqual(config["providerProfileName"], "deepinfra")
            self.assertEqual(config["provider"], "openai")
            self.assertEqual(config["openaiApiMode"], "chat_completions")
            self.assertEqual(config["openaiBaseUrl"], "https://api.deepinfra.com/v1/openai")
            self.assertEqual(config["openaiToken"], "deepinfra-test-key")
            self.assertEqual(config["model"], "zai-org/GLM-5")
        finally:
            if prev_key is None:
                os.environ.pop("DEEPINFRA_API_KEY", None)
            else:
                os.environ["DEEPINFRA_API_KEY"] = prev_key
            self.restore_home(root, previous)

    def test_resolve_config_anthropic_auth_separation_and_alt_screen(self):
        root, home, workspace, previous = self.temp_context()
        prev_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        prev_key = os.environ.get("ANTHROPIC_API_KEY")
        try:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-ant-o-test-token"
            token_config = run(core.resolveConfig({"provider": "anthropic"}, str(workspace)))
            self.assertEqual(token_config["providerProfileName"], "anthropic-claude-code")
            self.assertEqual(token_config["authToken"], "sk-ant-o-test-token")
            self.assertIsNone(token_config["apiKey"])

            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api-test-key"
            key_config = run(core.resolveConfig({"provider": "anthropic"}, str(workspace)))
            self.assertEqual(key_config["providerProfileName"], "anthropic-console")
            self.assertEqual(key_config["apiKey"], "sk-ant-api-test-key")
            self.assertIsNone(key_config["authToken"])

            self.assertEqual(run(core.resolveConfig({"altScreen": False}, str(workspace)))["screenMode"], "inline")
            self.assertEqual(run(core.resolveConfig({"noAltScreen": True}, str(workspace)))["screenMode"], "inline")
        finally:
            if prev_token is None:
                os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            else:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = prev_token
            if prev_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev_key
            self.restore_home(root, previous)

    def test_resolve_config_prefers_codex_oauth_for_codex_profile(self):
        root, home, workspace, previous = self.temp_context()
        prev_openai = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "openai-api-key-should-not-win"
        try:
            codex_auth_path = home / "codex-auth.json"
            codex_auth_path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "codex-access-token"}}))
            (home / "settings.json").write_text(json.dumps({"openaiCodexAuthPath": str(codex_auth_path)}))
            config = run(core.resolveConfig({"provider": "codex"}, str(workspace)))
            self.assertEqual(config["providerProfileName"], "codex")
            self.assertEqual(config["openaiApiMode"], "codex")
            self.assertEqual(config["openaiToken"], "codex-access-token")
        finally:
            if prev_openai is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = prev_openai
            self.restore_home(root, previous)

    def test_websearch_decodes_redirects_and_bing_fallback(self):
        root, home, workspace, previous = self.temp_context()
        original_fetch = tools_module.fetch
        try:
            async def ddg_fetch(*args, **kwargs):
                return Response(
                    """<!doctype html><html><body>
                    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdevelopers.openai.com%2Fapi%2Freference%2Fchat-completions%2Foverview&amp;rut=test">Chat</a>
                    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdevelopers.openai.com%2Fapi%2Freference%2Fchat-completions%2Foverview&amp;rut=test">Reference docs.</a>
                    </body></html>"""
                )

            tools_module.fetch = ddg_fetch
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home)}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode=config["permissionMode"])
            tool = next(tool for tool in core.createBuiltinTools() if tool.name == "WebSearch")
            result = run(tool.handler({"query": "OpenAI Chat Completions API", "allowed_domains": ["openai.com"]}, {"session": session, "config": config, "interactive": False, "abortSignal": None, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}))
            payload = json.loads(result["content"])
            self.assertEqual(payload["results"][0]["url"], "https://developers.openai.com/api/reference/chat-completions/overview")

            calls = {"count": 0}

            async def fallback_fetch(*args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    return Response('<div class="anomaly-modal__mask"></div>')
                return Response(
                    """<li class="b_algo">
                    <h2><a href="https://www.bing.com/ck/a?!&&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9yZXNlYXJjaA==">Research Result</a></h2>
                    <div class="b_caption"><p>Relevant fallback result.</p></div>
                    </li>"""
                )

            tools_module.fetch = fallback_fetch
            result = run(tool.handler({"query": "fallback test"}, {"session": session, "config": config, "interactive": False, "abortSignal": None, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}))
            payload = json.loads(result["content"])
            self.assertEqual(payload["results"][0]["url"], "https://example.com/research")
            self.assertEqual(calls["count"], 2)
        finally:
            tools_module.fetch = original_fetch
            self.restore_home(root, previous)

    def test_openai_chat_completions_params_and_streaming(self):
        params = testing.buildChatCompletionsCreateParams(
            {
                "model": "gpt-5.4",
                "effort": "xhigh",
                "system": "You are a coding agent.",
                "messages": [
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Write", "input": {"path": "a.txt"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "done"}]},
                ],
                "tools": [{"name": "Write", "description": "Write a file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}],
            }
        )
        self.assertEqual(params["reasoning_effort"], "xhigh")
        self.assertEqual(params["messages"][1]["tool_calls"][0]["function"]["arguments"], '{"path":"a.txt"}')

        params_no_effort = testing.buildChatCompletionsCreateParams({"model": "zai", "effort": "xhigh", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []}, {"includeReasoningEffort": False})
        self.assertNotIn("reasoning_effort", params_no_effort)

        emitted = []

        async def create(_params):
            return AsyncChunks(
                [
                    {"choices": [{"delta": {"content": "Planning"}, "finish_reason": None}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "Write", "arguments": '{"path":"'}}]}, "finish_reason": None}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'a.txt"}'}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 42, "completion_tokens": 7}},
                ]
            )

        response = run(
            testing.sendOpenAiChatCompletionsRequest(
                {
                    "token": "test-token",
                    "request": {"model": "gpt-5.4", "effort": "high", "system": "s", "messages": [{"role": "user", "content": "Write"}], "tools": []},
                    "runtime": {"includePartialMessages": True, "emitEvent": lambda event, data: emitted.append({"event": event, "data": data})},
                    "maxAttempts": 1,
                    "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
                }
            )
        )
        self.assertEqual(response["content"][0], {"type": "text", "text": "Planning"})
        self.assertEqual(response["content"][1]["input"], {"path": "a.txt"})
        self.assertEqual(response["usage"], {"input_tokens": 42, "output_tokens": 7})
        self.assertTrue(any(item["event"] == "message.assistant.partial" for item in emitted))

    def test_openai_reasoning_and_base_url(self):
        response = run(
            testing.sendOpenAiChatCompletionsRequest(
                {
                    "token": "test-token",
                    "baseUrl": "https://api.deepseek.com",
                    "request": {"model": "deepseek-v4-pro", "effort": "high", "system": "s", "messages": [{"role": "user", "content": "Read"}], "tools": []},
                    "runtime": {},
                    "maxAttempts": 1,
                    "clientFactory": lambda _options: SimpleNamespace(
                        chat=SimpleNamespace(
                            completions=SimpleNamespace(
                                create=lambda _params: AsyncChunks(
                                    [
                                        {"choices": [{"delta": {"reasoning_content": "Need to inspect the file."}, "finish_reason": None}]},
                                        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"path":"a.txt"}'}}]}, "finish_reason": "tool_calls"}]},
                                    ]
                                )
                            )
                        )
                    ),
                }
            )
        )
        self.assertEqual(response["content"][0]["provider_metadata"]["reasoning_content"], "Need to inspect the file.")

        captured = {}

        def factory(options):
            captured["base"] = options.get("baseURL")

            async def create(params):
                captured["params"] = params
                return AsyncChunks([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}])

            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        run(testing.sendOpenAiChatCompletionsRequest({"token": "test", "baseUrl": "http://127.0.0.1:8000/v1", "request": {"model": "m", "effort": "xhigh", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []}, "runtime": {}, "maxAttempts": 1, "clientFactory": factory}))
        self.assertEqual(captured["base"], "http://127.0.0.1:8000/v1")
        self.assertNotIn("reasoning_effort", captured["params"])

    def test_anthropic_payload_and_sse_normalization(self):
        request = {
            "model": "claude-sonnet-4-6",
            "effort": "high",
            "system": "system prompt",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "Read", "description": "Read files", "input_schema": {"type": "object"}}],
        }
        payload = testing.buildAnthropicMessagesPayload(request)
        self.assertEqual(payload["max_tokens"], 16384)
        self.assertEqual(payload["thinking"], {"type": "adaptive"})
        self.assertEqual(payload["output_config"], {"effort": "high"})
        self.assertEqual(payload["system"][1], {"type": "text", "text": "system prompt"})

        emitted = []
        raw = "\n\n".join(
            [
                'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":10}}}',
                'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi "}}',
                'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"Read","input":{}}}',
                'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"file_path\\":"}}',
                'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"a.txt\\"}"}}',
                'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}',
                'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":3}}',
                'event: message_stop\ndata: {"type":"message_stop"}',
            ]
        )
        normalized = testing.normalizeAnthropicSseResponse(raw, request, {"includePartialMessages": True, "emitEvent": lambda event, data: emitted.append((event, data))})
        self.assertEqual(normalized["content"][0], {"type": "text", "text": "Hi "})
        self.assertEqual(normalized["content"][1], {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "a.txt"}})
        self.assertEqual(normalized["usage"], {"input_tokens": 10, "output_tokens": 3})
        self.assertTrue(any(event == "message.assistant.partial" for event, _ in emitted))

    def test_responses_auth_failure_and_base_url(self):
        async def create_error(_params):
            raise RuntimeError("401 You have insufficient permissions for this operation. Missing scopes: api.responses.write.")

        with self.assertRaises(core.ProviderRequestError) as ctx:
            run(testing.sendOpenAiResponsesRequest({"token": "test", "mode": "codex", "request": {"model": "gpt-5.4", "effort": "high", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []}, "runtime": {}, "maxAttempts": 1, "clientFactory": lambda _options: SimpleNamespace(responses=SimpleNamespace(create=create_error))}))
        self.assertEqual(ctx.exception.record["category"], "auth")
        self.assertIn("authentication", str(ctx.exception).lower())

        captured = {}

        async def create_ok(_params):
            return AsyncChunks([{"type": "response.completed", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}], "usage": {"input_tokens": 3, "output_tokens": 1}}}])

        def response_factory(options):
            captured["base"] = options.get("baseURL")
            return SimpleNamespace(responses=SimpleNamespace(create=create_ok))

        run(testing.sendOpenAiResponsesRequest({"token": "test", "baseUrl": "http://127.0.0.1:8000/v1", "mode": "responses", "request": {"model": "m", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []}, "runtime": {}, "maxAttempts": 1, "clientFactory": response_factory}))
        self.assertEqual(captured["base"], "http://127.0.0.1:8000/v1")

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

    def test_fetch_proxy_and_compaction_model_defaults(self):
        self.assertEqual(core.resolveEnvironmentProxyForUrl("https://api.openai.com/v1/responses", {"HTTPS_PROXY": "https-proxy", "HTTP_PROXY": "http-proxy"}), "https-proxy")
        self.assertEqual(core.resolveEnvironmentProxyForUrl("https://api.openai.com/v1/responses", {"HTTP_PROXY": "http-proxy"}), "http-proxy")
        self.assertIsNone(core.resolveEnvironmentProxyForUrl("file:///tmp/a", {"HTTPS_PROXY": "https-proxy"}))

        session = core.createEmptySession(cwd="/tmp/workspace", provider="openai", providerProfileName="openai", model="gpt-5.4", effort="xhigh", systemPrompt="s", permissionMode="default")
        plan = testing.resolveCompactionExecutionPlan({"config": {}, "session": session})
        self.assertEqual(plan["synthesis"], {"provider": "openai", "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "fixed", "purpose": "synthesis"})
        self.assertEqual(plan["verification"], {"provider": "openai", "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "fixed", "purpose": "verification"})

    def test_runtime_state_reduces_provider_task_daemon_events(self):
        state = core.createInitialRuntimeState()
        state = core.reduceRuntimeState(state, {"type": "session.activated", "createdAt": "2026-01-01T00:00:00Z", "data": {"id": "s1", "cwd": "/tmp/w", "provider": "openai", "model": "gpt-5.4", "permissionMode": "default"}})
        self.assertEqual(state["session"]["activeSessionId"], "s1")
        state = core.reduceRuntimeState(state, {"type": "provider.input_tokens", "createdAt": "t1", "data": {"provider": "openai", "model": "gpt-5.4", "input_tokens": 123, "source": "estimate"}})
        self.assertEqual(state["provider"]["inputTokens"], 123)
        state = core.reduceRuntimeState(state, {"type": "daemon.snapshot", "createdAt": "t2", "data": {"pid": 42, "supervisorId": "sup", "activeTasks": 1}})
        self.assertEqual(state["daemon"]["phase"], "running")
        state = core.reduceRuntimeState(state, {"type": "task.registered", "createdAt": "t3", "data": {"task_id": "task-1", "kind": "background_command", "status": "running", "title": "test task"}})
        self.assertEqual(state["tasks"]["activeCount"], 1)
        state = core.reduceRuntimeState(state, {"type": "task.completed", "createdAt": "t4", "data": {"task_id": "task-1", "summary": "done"}})
        self.assertEqual(state["tasks"]["activeCount"], 0)
        self.assertEqual(state["tasks"]["tasks"][0]["status"], "completed")

    def test_codex_spark_compaction_uses_codex_transport(self):
        root, home, workspace, previous = self.temp_context()
        try:
            codex_auth_path = home / "codex-auth.json"
            codex_auth_path.write_text(json.dumps({"tokens": {"access_token": "codex-access-token"}}))
            captured = {}

            async def sender(args):
                captured.update(args)
                return {"content": [{"type": "text", "text": "Spark compaction output"}], "usage": {"input_tokens": 12, "output_tokens": 4}, "model": args["request"]["model"], "stopReason": "stop"}

            client = testing.createDefaultCompactionModelClient({"openAiRequestSender": sender})
            result = run(
                client.complete(
                    {
                        "config": {
                            "providerProfileName": "zai-glm-5-1",
                            "openaiApiMode": "chat_completions",
                            "openaiBaseUrl": "https://api.z.ai/api/paas/v4",
                            "openaiToken": "compatible-backend-token",
                            "customHeaders": {"x-test-provider": "zai"},
                            "openaiCodexAuthPath": str(codex_auth_path),
                        },
                        "model": {"provider": "openai", "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "fixed", "purpose": "synthesis"},
                        "system": "You are the compaction synthesizer.",
                        "prompt": "Summarize this transcript.",
                    }
                )
            )
            self.assertEqual(captured["mode"], "codex")
            self.assertEqual(captured["baseUrl"], testing.DEFAULT_CODEX_BASE_URL)
            self.assertEqual(captured["token"], "codex-access-token")
            self.assertIsNone(captured["headers"])
            self.assertEqual(result["text"], "Spark compaction output")
        finally:
            self.restore_home(root, previous)

    def test_background_task_supervisor_and_task_output(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            bash = next(tool for tool in core.createBuiltinTools() if tool.name == "Bash")
            script = 'print("hello from background")'
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            result = run(bash.handler({"command": command, "run_in_background": True}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}))
            task_id = json.loads(result["content"])["task_id"]
            daemon_state = run(core.getTaskSupervisorDaemonState())
            self.assertEqual(daemon_state["mode"], "detached_python")
            self.assertNotEqual(daemon_state["pid"], os.getpid())
            self.assertTrue(Path(daemon_state["socketPath"]).exists())
            self.assertTrue(run(core.connectTaskSupervisorDaemonClient())["_daemonClient"])
            for _ in range(40):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["status"], "completed")
            task_output = next(tool for tool in core.createBuiltinTools() if tool.name == "TaskOutput")
            output = run(task_output.handler({"task_id": task_id, "block": True, "timeout": 1000}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}))
            payload = json.loads(output["content"])
            self.assertIn("hello from background", payload["output"])
        finally:
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_daemon_live_subscription_receives_task_events(self):
        root, home, workspace, previous = self.temp_context()
        unsubscribe = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            client = run(core.ensureTaskSupervisorDaemon({"cwd": str(workspace)}))
            events = []
            unsubscribe = client["subscribe"](lambda event: events.append(event), session["id"])
            script = 'import time; print("subscription event"); time.sleep(0.2)'
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            started = run(client["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(workspace)}))
            task_id = started["taskId"]
            for _ in range(80):
                if any(event.get("event") == "task.completed" and event.get("data", {}).get("task_id") == task_id for event in events):
                    break
                time.sleep(0.05)
            task_events = [event for event in events if event.get("data", {}).get("task_id") == task_id]
            self.assertTrue(any(event.get("event") == "task.updated" for event in task_events))
            self.assertTrue(any(event.get("event") == "task.completed" for event in task_events))
            self.assertTrue(all(event.get("sessionId") == session["id"] for event in task_events))
        finally:
            if unsubscribe:
                unsubscribe()
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_daemon_reconciles_persisted_running_task_after_restart(self):
        root, home, workspace, previous = self.temp_context()
        proc = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "task-recovery"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("still running\n", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], cwd=str(workspace), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            created_at = "2026-01-01T00:00:00Z"
            meta = {
                "type": "background_command",
                "taskId": task_id,
                "id": task_id,
                "sessionId": "session-recovery",
                "command": "sleep",
                "cwd": str(workspace),
                "startedAt": created_at,
                "updatedAt": created_at,
                "status": "running",
                "outputFile": output_file,
                "pid": proc.pid,
                "detailStatus": "running",
                "owner": "daemon",
                "supervisorId": "previous-supervisor",
            }
            run(core.writeTaskMeta(task_id, meta))
            run(
                core.saveTaskRecord(
                    {
                        "id": task_id,
                        "sessionId": "session-recovery",
                        "sourceId": task_id,
                        "kind": "background_command",
                        "status": "running",
                        "title": "sleep",
                        "summary": "Background command is running.",
                        "createdAt": created_at,
                        "updatedAt": created_at,
                        "startedAt": created_at,
                        "cwd": str(workspace),
                        "command": "sleep",
                        "outputFile": output_file,
                        "outputCursor": len("still running\n"),
                        "foregroundMode": "background",
                        "taskFamily": "shell",
                        "detailStatus": "running",
                        "retrieveOnly": True,
                        "owner": "daemon",
                        "supervisorId": "previous-supervisor",
                        "metadata": {"pid": proc.pid},
                    }
                )
            )
            client = run(core.ensureTaskSupervisorDaemon({"cwd": str(workspace)}))
            record = None
            for _ in range(40):
                record = run(client["inspectTask"](task_id))
                if record and record.get("detailStatus") == "recovered_after_daemon_restart":
                    break
                time.sleep(0.05)
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], "degraded")
            self.assertEqual(record["detailStatus"], "recovered_after_daemon_restart")
            self.assertEqual(record["metadata"]["recovery_state"], "recovered_after_daemon_restart")
            next_meta = run(core.readTaskMeta(task_id))
            self.assertEqual(next_meta["status"], "reconciling")
            self.assertEqual(next_meta["detailStatus"], "recovered_after_daemon_restart")
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_task_stop_terminates_background_command(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            bash = next(tool for tool in core.createBuiltinTools() if tool.name == "Bash")
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(10)')}"
            result = run(bash.handler({"command": command, "run_in_background": True}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}))
            task_id = json.loads(result["content"])["task_id"]
            stopper = next(tool for tool in core.createBuiltinTools() if tool.name == "TaskStop")
            stopped = run(stopper.handler({"task_id": task_id}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}))
            self.assertFalse(stopped.get("isError"))
            for _ in range(20):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "stopped":
                    break
                time.sleep(0.05)
            self.assertEqual(run(core.readTaskMeta(task_id))["status"], "stopped")
        finally:
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_notebook_edit_replace_insert_delete(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            notebook_path = workspace / "demo.ipynb"
            notebook_path.write_text(json.dumps({"cells": [{"id": "cell-1", "cell_type": "code", "source": ["print(1)"], "metadata": {}, "outputs": []}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
            tool = next(tool for tool in core.createBuiltinTools() if tool.name == "NotebookEdit")
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}
            run(tool.handler({"notebook_path": str(notebook_path), "cell_id": "cell-1", "new_source": "print(2)"}, context))
            self.assertEqual(json.loads(notebook_path.read_text())["cells"][0]["source"], ["print(2)"])
            run(tool.handler({"notebook_path": str(notebook_path), "cell_id": "cell-1", "new_source": "print(3)", "edit_mode": "insert"}, context))
            self.assertEqual(len(json.loads(notebook_path.read_text())["cells"]), 2)
            run(tool.handler({"notebook_path": str(notebook_path), "cell_id": "cell-1", "new_source": "ignored", "edit_mode": "delete"}, context))
            cells = json.loads(notebook_path.read_text())["cells"]
            self.assertEqual(len(cells), 1)
            self.assertEqual(cells[0]["source"], ["print(3)"])
        finally:
            self.restore_home(root, previous)

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
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"serverInfo": {"name": "fake"}}})
                        elif method == "tools/list":
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "echo", "description": "Echo input", "inputSchema": {"type": "object"}}]}})
                        elif method == "tools/call":
                            args = request.get("params", {}).get("arguments", {})
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": "echo:" + args.get("value", "")}]}})
                        elif method == "resources/list":
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"resources": [{"uri": "memory://one", "name": "one", "mimeType": "text/plain"}]}})
                        elif method == "resources/read":
                            send({"jsonrpc": "2.0", "id": request_id, "result": {"contents": [{"uri": "memory://one", "mimeType": "text/plain", "text": "resource text"}]}})
                        else:
                            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                    '''
                )
            )
            runtime = run(core.createMcpRuntime({"cwd": str(workspace), "servers": [{"name": "fake", "transport": "stdio", "commandOrUrl": sys.executable, "args": [str(server)], "updatedAt": "now"}]}))
            self.assertEqual(runtime["listTools"]()[0]["runtimeName"], "mcp__fake__echo")
            called = run(runtime["callTool"]("fake", "echo", {"value": "ok"}))
            self.assertEqual(called["content"], "echo:ok")
            resources = run(runtime["listResources"]("fake"))
            self.assertEqual(resources[0]["uri"], "memory://one")
            resource = run(runtime["readResource"]("fake", "memory://one"))
            self.assertEqual(resource["contents"][0]["text"], "resource text")
            run(runtime["close"]())
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
