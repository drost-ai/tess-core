from __future__ import annotations

import asyncio
import contextlib
import http.server
import io
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import agent_core as core
from agent_core import testing
import agent_core.monitors as monitors_module
import agent_core.providers.anthropic as anthropic_module
import agent_core.providers.openai as openai_module
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
                    "runtime": {"includePartialMessages": True, "streamProviderMessages": True, "emitEvent": lambda event, data: emitted.append({"event": event, "data": data})},
                    "maxAttempts": 1,
                    "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
                }
            )
        )
        self.assertEqual(response["content"][0], {"type": "text", "text": "Planning"})
        self.assertEqual(response["content"][1]["input"], {"path": "a.txt"})
        self.assertEqual(response["usage"], {"input_tokens": 42, "output_tokens": 7})
        self.assertTrue(any(item["event"] == "message.assistant.partial" for item in emitted))
        self.assertTrue(any(item["event"] == "provider.request_started" and item["data"]["attempt"] == 1 for item in emitted))
        self.assertTrue(any(item["event"] == "provider.stream_event" and item["data"]["stream_event"] == "chat.completion.chunk" for item in emitted))

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
        self.assertNotIn("stream", testing.buildAnthropicMessagesPayload({**request, "effort": None}))

        with self.assertRaises(core.ProviderRequestError) as ctx:
            testing.normalizeAnthropicSseResponse('event: error\ndata: {"type":"error","error":{"type":"rate_limit_error","message":"rate limit"}}', request)
        self.assertEqual(ctx.exception.record["category"], "rate_limit")
        self.assertTrue(ctx.exception.record["retryable"])

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

    def test_openai_retry_events_for_responses_and_chat(self):
        original_delay = openai_module.providerRetryDelayMs
        openai_module.providerRetryDelayMs = lambda _attempt: 0
        try:
            response_attempts = {"count": 0}
            response_events = []

            async def create_response(_params):
                response_attempts["count"] += 1
                if response_attempts["count"] == 1:
                    raise RuntimeError("socket hang up")
                return AsyncChunks([{"type": "response.completed", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}}])

            response = run(
                testing.sendOpenAiResponsesRequest(
                    {
                        "token": "test",
                        "mode": "responses",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"emitEvent": lambda event, data: response_events.append((event, data))},
                        "maxAttempts": 2,
                        "clientFactory": lambda _options: SimpleNamespace(responses=SimpleNamespace(create=create_response)),
                    }
                )
            )
            self.assertEqual(response["content"], [{"type": "text", "text": "ok"}])
            self.assertEqual(response_attempts["count"], 2)
            self.assertEqual([data["attempt"] for event, data in response_events if event == "provider.request_started"], [1, 2])
            self.assertTrue(any(event == "provider.retrying" and data["category"] == "transport" and data["next_attempt"] == 2 for event, data in response_events))
            self.assertTrue(any(event == "provider.recovered" and data["attempts"] == 2 and data["category"] == "transport" for event, data in response_events))

            chat_attempts = {"count": 0}
            chat_events = []

            async def create_chat(_params):
                chat_attempts["count"] += 1
                if chat_attempts["count"] == 1:
                    raise RuntimeError("ECONNRESET")
                return AsyncChunks([{"choices": [{"delta": {"content": "chat-ok"}, "finish_reason": "stop"}]}])

            chat_response = run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "test",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"streamProviderMessages": True, "emitEvent": lambda event, data: chat_events.append((event, data))},
                        "maxAttempts": 2,
                        "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_chat))),
                    }
                )
            )
            self.assertEqual(chat_response["content"], [{"type": "text", "text": "chat-ok"}])
            self.assertEqual(chat_attempts["count"], 2)
            self.assertTrue(any(event == "provider.retrying" and data["category"] == "transport" for event, data in chat_events))
            self.assertTrue(any(event == "provider.recovered" and data["attempts"] == 2 for event, data in chat_events))
            self.assertTrue(any(event == "provider.stream_event" and data["stream_event"] == "chat.completion.chunk" for event, data in chat_events))
        finally:
            openai_module.providerRetryDelayMs = original_delay

    def test_anthropic_count_tokens_measurement_and_fallback(self):
        root, home, workspace, previous = self.temp_context()
        calls = {"count": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                calls["count"] += 1
                length = int(self.headers.get("content-length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                assert self.path == "/v1/messages/count_tokens"
                assert payload["model"] == "claude-sonnet-4-6"
                if calls["count"] == 1:
                    body = json.dumps({"input_tokens": 77}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(500)
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "hello"}], "tools": []}
            config = {"provider": "anthropic", "anthropicBaseUrl": f"http://127.0.0.1:{server.server_address[1]}/v1", "apiKey": "test-key", "customHeaders": {}}
            measured = run(core.measureRequestInputTokens({"config": config, "request": request, "session": {}, "timeoutSeconds": 2}))
            self.assertEqual(measured["inputTokens"], 77)
            self.assertEqual(measured["source"], "anthropic_count")
            fallback = run(core.measureRequestInputTokens({"config": config, "request": request, "session": {}, "timeoutSeconds": 2}))
            self.assertEqual(fallback["source"], "legacy_estimate")
            self.assertGreater(fallback["inputTokens"], 0)
        finally:
            server.shutdown()
            server.server_close()
            self.restore_home(root, previous)

    def test_openai_chat_request_start_timeout(self):
        async def create_slow(_params):
            await asyncio.sleep(0.05)
            return AsyncChunks([])

        with self.assertRaises(core.ProviderRequestError) as ctx:
            run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "test",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {},
                        "maxAttempts": 1,
                        "requestStartTimeoutMs": 1,
                        "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_slow))),
                    }
                )
            )
        self.assertEqual(ctx.exception.record["category"], "timeout")
        self.assertEqual(ctx.exception.record["phase"], "request_start")
        self.assertTrue(ctx.exception.record["retryable"])

    def test_openai_chat_stream_idle_timeout(self):
        class SlowStream:
            async def __aiter__(self):
                await asyncio.sleep(0.05)
                yield {"choices": [{"delta": {"content": "late"}, "finish_reason": "stop"}]}

        async def create_stream(_params):
            return SlowStream()

        with self.assertRaises(core.ProviderRequestError) as ctx:
            run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "test",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {},
                        "maxAttempts": 1,
                        "requestStartTimeoutMs": 1000,
                        "streamIdleTimeoutMs": 1,
                        "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_stream))),
                    }
                )
            )
        self.assertEqual(ctx.exception.record["category"], "timeout")
        self.assertEqual(ctx.exception.record["phase"], "stream_open")
        self.assertTrue(ctx.exception.record["retryable"])

    def test_anthropic_messages_request_timeout(self):
        original_send = anthropic_module._send_anthropic_messages_sync

        def slow_send(*_args):
            time.sleep(0.05)
            return "{}", "application/json"

        anthropic_module._send_anthropic_messages_sync = slow_send
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(
                    core.sendAnthropicMessagesRequest(
                        {
                            "config": {"provider": "anthropic", "apiKey": "test-key", "customHeaders": {}},
                            "request": {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                            "runtime": {},
                            "jsonResponseTimeoutMs": 1,
                        }
                    )
                )
            self.assertEqual(ctx.exception.record["category"], "timeout")
            self.assertEqual(ctx.exception.record["phase"], "request_start")
            self.assertTrue(ctx.exception.record["retryable"])
        finally:
            anthropic_module._send_anthropic_messages_sync = original_send

    def test_provider_requests_respect_abort_signal(self):
        signal = SimpleNamespace(aborted=True)
        calls = {"openai": 0, "anthropic": 0}
        openai_events = []

        def openai_factory(_options):
            calls["openai"] += 1

            async def create(_params):
                return AsyncChunks([])

            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with self.assertRaises(core.ProviderRequestError) as openai_ctx:
            run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "test",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"abortSignal": signal, "emitEvent": lambda event, data: openai_events.append((event, data))},
                        "maxAttempts": 2,
                        "clientFactory": openai_factory,
                    }
                )
            )
        self.assertEqual(openai_ctx.exception.record["category"], "cancelled")
        self.assertFalse(openai_ctx.exception.record["retryable"])
        self.assertEqual(calls["openai"], 0)
        self.assertFalse(any(event == "provider.request_started" for event, _ in openai_events))

        original_send = anthropic_module._send_anthropic_messages_sync
        anthropic_events = []

        def send_anthropic(*_args):
            calls["anthropic"] += 1
            return "{}", "application/json"

        anthropic_module._send_anthropic_messages_sync = send_anthropic
        try:
            with self.assertRaises(core.ProviderRequestError) as anthropic_ctx:
                run(
                    core.sendAnthropicMessagesRequest(
                        {
                            "config": {"provider": "anthropic", "apiKey": "test-key", "customHeaders": {}},
                            "request": {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                            "runtime": {"abortSignal": signal, "emitEvent": lambda event, data: anthropic_events.append((event, data))},
                            "maxAttempts": 2,
                        }
                    )
                )
            self.assertEqual(anthropic_ctx.exception.record["category"], "cancelled")
            self.assertFalse(anthropic_ctx.exception.record["retryable"])
            self.assertEqual(calls["anthropic"], 0)
            self.assertFalse(any(event == "provider.request_started" for event, _ in anthropic_events))
        finally:
            anthropic_module._send_anthropic_messages_sync = original_send

    def test_provider_requests_cancel_when_abort_signal_flips_during_activity(self):
        openai_signal = SimpleNamespace(aborted=False)
        calls = {"openai": 0, "anthropic": 0}

        async def create_slow(_params):
            calls["openai"] += 1
            openai_signal.aborted = True
            await asyncio.sleep(1)
            return AsyncChunks([])

        with self.assertRaises(core.ProviderRequestError) as openai_ctx:
            run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "test",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"abortSignal": openai_signal},
                        "maxAttempts": 2,
                        "requestStartTimeoutMs": 1000,
                        "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_slow))),
                    }
                )
            )
        self.assertEqual(openai_ctx.exception.record["category"], "cancelled")
        self.assertFalse(openai_ctx.exception.record["retryable"])
        self.assertEqual(calls["openai"], 1)

        anthropic_signal = SimpleNamespace(aborted=False)
        original_send = anthropic_module._send_anthropic_messages_sync

        def slow_send(*_args):
            calls["anthropic"] += 1
            anthropic_signal.aborted = True
            time.sleep(0.2)
            return "{}", "application/json"

        anthropic_module._send_anthropic_messages_sync = slow_send
        try:
            with self.assertRaises(core.ProviderRequestError) as anthropic_ctx:
                run(
                    core.sendAnthropicMessagesRequest(
                        {
                            "config": {"provider": "anthropic", "apiKey": "test-key", "customHeaders": {}},
                            "request": {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                            "runtime": {"abortSignal": anthropic_signal},
                            "maxAttempts": 2,
                            "jsonResponseTimeoutMs": 1000,
                        }
                    )
                )
            self.assertEqual(anthropic_ctx.exception.record["category"], "cancelled")
            self.assertFalse(anthropic_ctx.exception.record["retryable"])
            self.assertEqual(calls["anthropic"], 1)
        finally:
            anthropic_module._send_anthropic_messages_sync = original_send

    def test_anthropic_http_retry_and_non_retry_classification(self):
        original_delay = anthropic_module.providerRetryDelayMs
        anthropic_module.providerRetryDelayMs = lambda _attempt: 0
        retry_calls = {"count": 0, "accept": None, "payload": None}

        class RetryHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                retry_calls["count"] += 1
                retry_calls["accept"] = self.headers.get("accept")
                length = int(self.headers.get("content-length") or 0)
                retry_calls["payload"] = json.loads(self.rfile.read(length).decode("utf-8"))
                if retry_calls["count"] == 1:
                    body = json.dumps({"error": {"type": "overloaded_error", "message": "temporary"}}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 5, "output_tokens": 1}, "stop_reason": "end_turn"}).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        retry_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
        retry_thread = threading.Thread(target=retry_server.serve_forever, daemon=True)
        retry_thread.start()
        try:
            events = []
            request = {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "hello"}], "tools": []}
            config = {"provider": "anthropic", "anthropicBaseUrl": f"http://127.0.0.1:{retry_server.server_address[1]}/v1", "apiKey": "test-key", "customHeaders": {}}
            response = run(core.sendAnthropicMessagesRequest({"config": config, "request": request, "runtime": {"emitEvent": lambda event, data: events.append((event, data))}, "maxAttempts": 2}))
            self.assertEqual(response["content"], [{"type": "text", "text": "ok"}])
            self.assertEqual(retry_calls["count"], 2)
            self.assertEqual(retry_calls["accept"], "application/json")
            self.assertNotIn("stream", retry_calls["payload"])
            self.assertTrue(any(event == "provider.retrying" and data["category"] == "server" for event, data in events))
            self.assertTrue(any(event == "provider.recovered" and data["attempts"] == 2 for event, data in events))
        finally:
            retry_server.shutdown()
            retry_server.server_close()

        auth_calls = {"count": 0}

        class AuthHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                auth_calls["count"] += 1
                body = json.dumps({"error": {"type": "authentication_error", "message": "bad key"}}).encode("utf-8")
                self.send_response(401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        auth_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler)
        auth_thread = threading.Thread(target=auth_server.serve_forever, daemon=True)
        auth_thread.start()
        try:
            request = {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "hello"}], "tools": []}
            config = {"provider": "anthropic", "anthropicBaseUrl": f"http://127.0.0.1:{auth_server.server_address[1]}/v1", "apiKey": "test-key", "customHeaders": {}}
            with self.assertRaises(core.ProviderRequestError) as auth_ctx:
                run(core.sendAnthropicMessagesRequest({"config": config, "request": request, "runtime": {}, "maxAttempts": 2}))
            self.assertEqual(auth_ctx.exception.record["category"], "auth")
            self.assertEqual(auth_calls["count"], 1)
        finally:
            auth_server.shutdown()
            auth_server.server_close()
            anthropic_module.providerRetryDelayMs = original_delay

    def test_provider_failure_classifier_matrix_matches_reference(self):
        base = {"provider": "openai", "providerLabel": "OpenAI", "phase": "request_start", "attempt": 1, "hadAssistantOutput": False, "hadToolUse": False}
        cases = [
            (401, "invalid api key", None, "auth", False),
            (403, "forbidden", None, "auth", False),
            (408, "timeout", None, "timeout", True),
            (429, "rate limit exceeded", None, "rate_limit", True),
            (429, "insufficient quota or billing", None, "quota", False),
            (400, "bad request", None, "client_request", False),
            (500, "server overloaded", None, "server", True),
        ]
        for status, body, error_type, category, retryable in cases:
            with self.subTest(status=status, category=category):
                failure = core.classifyProviderHttpFailure({**base, "status": status, "body": body, "errorType": error_type})
                self.assertEqual(failure.record["category"], category)
                self.assertEqual(failure.record["retryable"], retryable)
                self.assertEqual(failure.record["status"], status)
                self.assertEqual(failure.record["rawBody"], body)

        transport = core.normalizeUnknownProviderFailure({**base, "error": RuntimeError("socket hang up")})
        self.assertEqual(transport.record["category"], "transport")
        self.assertTrue(core.canRetryProviderFailure(transport, 2))
        partial = core.normalizeUnknownProviderFailure({**base, "error": RuntimeError("ECONNRESET"), "hadAssistantOutput": True})
        self.assertEqual(partial.record["category"], "transport")
        self.assertFalse(core.canRetryProviderFailure(partial, 2))
        protocol = core.normalizeUnknownProviderFailure({**base, "error": RuntimeError("Unexpected end of JSON input")})
        self.assertEqual(protocol.record["category"], "protocol")
        self.assertFalse(protocol.record["retryable"])
        auth = core.normalizeUnknownProviderFailure({**base, "error": RuntimeError("invalid API key")})
        self.assertEqual(auth.record["category"], "auth")

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

    def test_runtime_state_matches_typed_event_projection_edges(self):
        state = core.createInitialRuntimeState()
        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "task.registered", "createdAt": "t1", "data": {"task_id": "health-1", "kind": "health_check", "status": "running", "title": "API health"}}))
        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "health_check.unhealthy", "createdAt": "t2", "data": {"task_id": "health-1", "title": "API health", "error": "500"}}))
        self.assertEqual(state["tasks"]["tasks"][0]["status"], "degraded")
        self.assertEqual(state["tasks"]["failedCount"], 1)
        self.assertIn("500", state["tasks"]["tasks"][0]["summary"])

        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "health_check.recovered", "createdAt": "t3", "data": {"task_id": "health-1", "title": "API health"}}))
        self.assertEqual(state["tasks"]["tasks"][0]["status"], "running")
        self.assertEqual(state["tasks"]["failedCount"], 0)

        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "collaboration.assistant_update", "createdAt": "t4", "data": {"source_kind": "monitor", "source_id": "health-1", "status": "running", "label": "API health", "text": "API recovered."}}))
        self.assertEqual(state["collaboration"]["recentAssistantUpdates"][0]["sourceKind"], "monitor")
        self.assertEqual(state["collaboration"]["recentAssistantUpdates"][0]["text"], "API recovered.")

        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "bridge.message", "createdAt": "t5", "data": {"bridge_id": "bridge-1", "remote_session_id": "remote-1", "text": "continue"}}))
        self.assertTrue(state["bridge"]["attached"])
        self.assertEqual(state["bridge"]["lastEventName"], "bridge.message")
        self.assertEqual(state["collaboration"]["pendingBridgeMessages"], 1)

        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "bridge.closed", "createdAt": "t6", "data": {"bridge_id": "bridge-1", "remote_session_id": "remote-1"}}))
        self.assertFalse(state["bridge"]["attached"])
        self.assertEqual(state["bridge"]["lastEventName"], "bridge.closed")
        self.assertEqual(state["collaboration"]["bridgeStatus"], "closed")

    def test_bootstrap_runtime_exposes_typed_event_and_turn_lifecycle_surface(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            boot = run(core.bootstrapRuntime({"session": session, "config": config}))
            self.assertEqual(boot["getState"]()["session"]["activeSessionId"], session["id"])
            envelopes = []
            unsubscribe = boot["subscribeEvents"](envelopes.append)
            boot["startTurn"]({"prompt": "build", "agentName": "main"})
            self.assertEqual(boot["getState"]()["turn"]["status"], "running")
            self.assertEqual(envelopes[-1]["typedEvent"]["kind"], "turn.started")
            boot["finishTurn"]({"kind": "completed", "stopReason": "stop", "message": "done"})
            self.assertEqual(boot["getState"]()["turn"]["lastOutcome"]["message"], "done")
            other = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="acceptEdits")
            boot["setActiveSession"](other, {**config, "permissionMode": "acceptEdits"})
            self.assertEqual(boot["getState"]()["session"]["activeSessionId"], other["id"])
            self.assertEqual(envelopes[-1]["typedEvent"]["kind"], "session.activated")
            unsubscribe()
            run(boot["close"]())
        finally:
            self.restore_home(root, previous)

    def test_collaboration_runtime_projects_workers_and_bridge_state(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child = {
                "agentId": "agent-collab",
                "sessionId": "child-session",
                "parentSessionId": session["id"],
                "name": "Delegate",
                "provider": "openai",
                "model": "gpt-5.4",
                "mode": "bypassPermissions",
                "runInBackground": True,
                "status": "backgrounded",
                "startedAt": "2026-01-01T00:00:00Z",
                "taskId": "task-subagent",
                "owner": "daemon",
                "attachableSessionId": "child-session",
                "lastUpdatedAt": "2026-01-01T00:00:01Z",
                "lastSummary": "Running in the background.",
                "stoppable": True,
            }
            run(core.saveChildAgent(child))
            session["metadata"]["childAgentIds"] = [child["agentId"]]
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": "task-monitor", "goal": "Watch tests"})
            monitor["lastAssistantUpdateSummary"] = "Tests are still running."
            run(core.saveMonitor(monitor))
            remote = run(core.createRemoteSession({"session": session, "description": "Remote QA", "channelServers": ["local"]}))
            session["metadata"]["remoteSessionId"] = remote["id"]
            bridge_events = []
            bridge_runtime = run(core.createBridgeRuntime({"session": session, "remoteSessionId": remote["id"], "name": "qa", "startPolling": False, "emit": lambda event, data: bridge_events.append((event, data))}))
            session["metadata"]["bridgeId"] = bridge_runtime["bridge"]["id"]
            message = run(core.sendBridgeMessage({"bridgeId": bridge_runtime["bridge"]["id"], "text": "please review", "source": "remote"}))
            self.assertEqual(message["source"], "remote")
            bridge = run(core.loadBridge(bridge_runtime["bridge"]["id"]))
            self.assertEqual(bridge["pendingMessages"], 1)
            self.assertEqual(bridge["lastEventName"], "bridge.message.queued")
            remote_after_message = run(core.loadRemoteSession(remote["id"]))
            self.assertEqual(remote_after_message["status"], "awaiting_input")
            self.assertEqual(remote_after_message["unreadBridgeMessages"], 1)
            self.assertIn("bridge.message.queued", run(core.formatBridgeTail(bridge["id"])))

            events = []
            runtime = run(core.createCollaborationRuntime({"session": session, "config": config, "emit": lambda event, data: events.append((event, data))}))
            snapshot = runtime["getSnapshot"]()
            self.assertEqual(snapshot["activeCount"], 2)
            self.assertEqual(snapshot["awaitingInputCount"], 1)
            self.assertEqual(snapshot["backgroundCount"], 2)
            self.assertEqual(snapshot["pendingBridgeMessages"], 1)
            self.assertIn("Delegate", snapshot["labels"])
            workers = {worker["kind"]: worker for worker in snapshot["workers"]}
            self.assertEqual(workers["subagent"]["status"], "backgrounded")
            self.assertEqual(workers["subagent"]["taskId"], "task-subagent")
            self.assertEqual(workers["remote"]["status"], "awaiting_input")
            self.assertTrue(any(event == "collaboration.snapshot" and data["pending_bridge_messages"] == 1 for event, data in events))
            self.assertTrue(any(event == "subagent.status_changed" and data["agent_id"] == child["agentId"] for event, data in events))
            self.assertTrue(any(event == "monitor.status_changed" and data["monitor_id"] == monitor["id"] for event, data in events))
            self.assertTrue(any(event == "remote.status_changed" and data["remote_session_id"] == remote["id"] for event, data in events))
            self.assertTrue(any(event == "bridge.status_changed" and data["pending_messages"] == 1 for event, data in events))

            state = core.createInitialRuntimeState(session)
            for event, data in events:
                state = core.reduceRuntimeState(state, {"type": event, "createdAt": "2026-01-01T00:00:02Z", "data": data})
            self.assertEqual(state["collaboration"]["backgroundCount"], 2)
            self.assertEqual(state["collaboration"]["pendingBridgeMessages"], 1)
            self.assertTrue(state["collaboration"]["recentEvents"])

            boot = run(core.bootstrapRuntime({"session": session, "config": config}))
            self.assertTrue(boot["collaborationRuntime"])
            self.assertTrue(any(tool.name == "Agent" for tool in boot["tools"]))
            self.assertGreaterEqual(boot["stateStore"]["getState"]()["collaboration"]["activeCount"], 2)
            run(boot["close"]())
            run(runtime["close"]())
            run(bridge_runtime["close"]())
            closed = run(core.loadBridge(bridge["id"]))
            self.assertEqual(closed["status"], "closed")
        finally:
            self.restore_home(root, previous)

    def test_bridge_runtime_polls_inbox_and_records_received_event(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            remote = run(core.createRemoteSession({"session": session, "description": "Remote inbox QA", "channelServers": ["local"]}))
            bridge_events = []
            bridge_runtime = run(core.createBridgeRuntime({"session": session, "remoteSessionId": remote["id"], "name": "qa", "startPolling": False, "emit": lambda event, data: bridge_events.append((event, data))}))
            message = run(core.sendBridgeMessage({"bridgeId": bridge_runtime["bridge"]["id"], "text": "please replay", "source": "remote"}))

            delivered = run(bridge_runtime["pollInbox"]())
            self.assertEqual([item["id"] for item in delivered], [message["id"]])
            self.assertTrue(any(event == "bridge.message" and data["message_id"] == message["id"] and data["text"] == "please replay" for event, data in bridge_events))
            bridge = run(core.loadBridge(bridge_runtime["bridge"]["id"]))
            self.assertEqual(bridge["pendingMessages"], 0)
            self.assertEqual(bridge["messageCount"], 1)
            self.assertEqual(bridge["lastEventName"], "bridge.message.received")
            remote_after_receive = run(core.loadRemoteSession(remote["id"]))
            self.assertEqual(remote_after_receive["lastEventName"], "bridge.message.received")
            self.assertIn("Bridge message: please replay", remote_after_receive["lastEventSummary"])
            self.assertIn("bridge.message.received", run(core.formatBridgeTail(bridge["id"])))

            self.assertEqual(run(bridge_runtime["pollInbox"]()), [])
            bridge_after_repoll = run(core.loadBridge(bridge_runtime["bridge"]["id"]))
            self.assertEqual(bridge_after_repoll["messageCount"], 1)
            run(bridge_runtime["close"]())
            closed_remote = run(core.loadRemoteSession(remote["id"]))
            self.assertEqual(closed_remote["bridgeStatus"], "closed")
        finally:
            self.restore_home(root, previous)

    def test_bridge_tail_and_truncation_match_reference_shape(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            remote = run(core.createRemoteSession({"session": session, "description": "Remote diagnostics QA", "channelServers": ["local"]}))
            bridge_runtime = run(core.createBridgeRuntime({"session": session, "remoteSessionId": remote["id"], "name": "qa", "startPolling": False, "emit": lambda *_: None}))
            run(bridge_runtime["recordEvent"]("bridge.diagnostic", {"summary": "diagnostic", "count": 2}))
            tail = run(core.formatBridgeTail(bridge_runtime["bridge"]["id"]))
            self.assertIn('bridge.diagnostic  {"summary":"diagnostic","count":2}', tail)

            long_text = "x" * 120
            run(core.sendBridgeMessage({"bridgeId": bridge_runtime["bridge"]["id"], "text": long_text, "source": "remote"}))
            remote_after_queue = run(core.loadRemoteSession(remote["id"]))
            self.assertTrue(remote_after_queue["lastEventSummary"].endswith("\u2026"))
            self.assertEqual(len(remote_after_queue["lastEventSummary"]), 96)
            run(bridge_runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_collaboration_runtime_authors_deterministic_assistant_update(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"].append({"role": "user", "content": "Track the delegated work."})
            child = {
                "agentId": "agent-collab-update",
                "sessionId": "child-update-session",
                "parentSessionId": session["id"],
                "name": "Delegate",
                "provider": "openai",
                "model": "gpt-5.4",
                "mode": "bypassPermissions",
                "runInBackground": True,
                "status": "completed",
                "startedAt": "2026-01-01T00:00:00Z",
                "completedAt": "2026-01-01T00:00:02Z",
                "lastUpdatedAt": "2026-01-01T00:00:02Z",
                "resultSummary": "Collected the evidence.",
            }
            run(core.saveChildAgent(child))
            session["metadata"]["childAgentIds"] = [child["agentId"]]
            events = []
            runtime = run(core.createCollaborationRuntime({"session": session, "config": config, "minAssistantUpdateSpacingMs": 0, "emit": lambda event, data: events.append((event, data))}))
            update = next(data for event, data in events if event == "collaboration.assistant_update")
            self.assertEqual(update["source_kind"], "subagent")
            self.assertEqual(update["source_id"], child["agentId"])
            self.assertIn("finished its background work", update["text"])
            self.assertIn("Collected the evidence.", update["text"])
            self.assertEqual(session["messages"][-1]["role"], "assistant")
            self.assertEqual(session["messages"][-1]["content"], update["text"])
            persisted = run(core.loadSession(session["id"]))
            self.assertEqual(persisted["messages"][-1]["content"], update["text"])
            run(runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_slash_command_registry_and_core_handlers(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            session["todos"] = [{"content": "ship shell slice", "status": "in_progress"}]
            infos = []
            errors = []
            counters = {"persist": 0, "refresh": 0, "clear": 0}
            hooks = {
                "info": lambda title, message: infos.append((title, message)),
                "error": lambda message: errors.append(message),
                "persist": lambda: counters.__setitem__("persist", counters["persist"] + 1),
                "refresh": lambda: counters.__setitem__("refresh", counters["refresh"] + 1),
                "clear": lambda: counters.__setitem__("clear", counters["clear"] + 1),
                "getTheme": lambda: "tess",
                "setTheme": lambda _theme: None,
            }

            runtime_seen = {}

            def hello_handler(payload):
                runtime_seen["hello"] = payload
                return "hello from plugin"

            def status_override(payload):
                runtime_seen["status_override"] = payload
                return "plugin status"

            runtime_commands = [{"name": "hello", "usage": "/hello", "description": "Say hello", "handler": hello_handler}, {"name": "status", "usage": "/status", "description": "Should not override builtin", "handler": status_override}]
            commands = core.listSlashCommands({"includeTheme": True, "runtimeCommands": runtime_commands})
            names = [command["name"] for command in commands]
            self.assertIn("provider", names)
            self.assertIn("tasks", names)
            self.assertIn("theme", names)
            self.assertIn("hello", names)

            help_result = run(core.handleSlashCommand({"command": "/help", "session": session, "config": config, "hooks": hooks, "runtimeCommands": runtime_commands}))
            self.assertEqual(help_result["outcome"], "handled")
            self.assertIn("/provider [name]", help_result["message"])
            self.assertIn("/hello", help_result["message"])

            self.assertEqual(run(core.handleSlashCommand({"command": "plain text"}))["outcome"], "pass")
            self.assertEqual(run(core.handleSlashCommand({"command": "/hello plugin args", "session": session, "config": config, "hooks": hooks, "runtimeCommands": runtime_commands}))["message"], "hello from plugin")
            self.assertEqual(runtime_seen["hello"]["argText"], "plugin args")
            self.assertEqual(runtime_seen["hello"]["args"], "plugin args")
            run(core.handleSlashCommand({"command": "/clear", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(counters["clear"], 1)

            status = run(core.handleSlashCommand({"command": "/status", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("session", status["message"])
            self.assertNotIn("status_override", runtime_seen)
            self.assertEqual(infos[-1][0], "Status")
            providers_report = run(core.handleSlashCommand({"command": "/providers", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("openai  (active)", providers_report["message"])
            self.assertIn("mode       responses", providers_report["message"])
            self.assertIn("auth       openai_api_key via OPENAI_API_KEY", providers_report["message"])
            self.assertIn("about      Official OpenAI Responses API", providers_report["message"])

            run(core.handleSlashCommand({"command": "/model openai:gpt", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(session["provider"], "openai")
            self.assertEqual(session["model"], "gpt-5.4")
            run(core.handleSlashCommand({"command": "/effort xhigh", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(session["effort"], "xhigh")
            run(core.handleSlashCommand({"command": "/permissions auto", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(session["permissionMode"], "auto")
            self.assertGreaterEqual(counters["persist"], 3)
            self.assertGreaterEqual(counters["refresh"], 3)

            policy = run(core.handleSlashCommand({"command": "/permissions policy", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("auto allow", policy["message"])
            run(core.handleSlashCommand({"command": "/permissions allow add python -m pytest", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": "/permissions deny add rm -rf build", "session": session, "config": config, "hooks": hooks}))
            auto_mode = run(core.loadAutoMode(str(workspace)))
            self.assertIn("python -m pytest", auto_mode["allow"])
            self.assertIn("rm -rf build", auto_mode["deny"])
            run(core.handleSlashCommand({"command": "/permissions allow remove python -m pytest", "session": session, "config": config, "hooks": hooks}))
            self.assertNotIn("python -m pytest", run(core.loadAutoMode(str(workspace)))["allow"])
            run(core.handleSlashCommand({"command": "/permissions reset", "session": session, "config": config, "hooks": hooks}))
            self.assertNotIn("rm -rf build", run(core.loadAutoMode(str(workspace)))["deny"])

            provider_result = run(core.handleSlashCommand({"command": "/provider anthropic-console", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("anthropic-console", provider_result["message"])
            self.assertEqual(session["provider"], "anthropic")
            self.assertEqual(config["providerProfileName"], "anthropic-console")
            bad_provider = run(core.handleSlashCommand({"command": "/provider missing-profile", "session": session, "config": config, "hooks": hooks}))
            self.assertTrue(bad_provider["isError"])
            self.assertIn("Unknown provider profile", errors[-1])

            todos = run(core.handleSlashCommand({"command": "/todos", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("ship shell slice", todos["message"])
            usage = run(core.handleSlashCommand({"command": "/cost", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("inputTokens", usage["message"])

            run(core.saveChildAgent({"agentId": "agent-shell", "sessionId": "child-shell", "parentSessionId": session["id"], "name": "Shell worker", "provider": "openai", "model": "gpt-5.4", "mode": "auto", "runInBackground": True, "status": "backgrounded", "startedAt": "2026-01-01T00:00:00Z", "lastSummary": "working"}))
            session["metadata"]["childAgentIds"] = ["agent-shell"]
            agents = run(core.handleSlashCommand({"command": "/agents running", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("agent-shell", agents["message"])

            run(core.saveTaskRecord({"id": "task-shell", "sessionId": session["id"], "kind": "background_command", "status": "running", "title": "Shell task", "updatedAt": "2026-01-01T00:00:00Z"}))
            tasks = run(core.handleSlashCommand({"command": "/tasks", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("task-shell", tasks["message"])
            inspected = run(core.handleSlashCommand({"command": "/tasks inspect task-shell", "session": session, "config": config, "hooks": hooks}))
            self.assertIn('"kind": "background_command"', inspected["message"])

            run(core.saveDaemonState({"phase": "running", "pid": 123, "socketPath": str(workspace / "daemon.sock"), "supervisorId": "sup", "epoch": 1, "attachedClients": 0, "watchedSessions": 1, "activeTasks": 1, "updatedAt": "2026-01-01T00:00:00Z"}))
            daemon = run(core.handleSlashCommand({"command": "/daemon", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("running", daemon["message"])

            exit_result = run(core.handleSlashCommand({"command": "/quit", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(exit_result["outcome"], "exit")
        finally:
            self.restore_home(root, previous)

    def test_shell_commands_memory_monitor_resume_compact_review_and_tui(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            infos = []
            errors = []
            events = []
            resumed = []
            prompts = []
            compaction_calls = []
            tui_state = {"statusline": True, "theme": "tess", "mouse": "selection", "screen": "auto", "persist": 0, "refresh": 0}

            def fake_compaction(payload):
                compaction_calls.append(payload)
                return {"status": "skipped", "reason": "below_threshold", "detail": "not enough transcript"}

            def fake_run_prompt(payload):
                prompts.append(payload)
                return {"text": "review queued", "stopReason": "stop"}

            hooks = {
                "info": lambda title, message: infos.append((title, message)),
                "error": lambda message: errors.append(message),
                "persist": lambda: tui_state.__setitem__("persist", tui_state["persist"] + 1),
                "refresh": lambda: tui_state.__setitem__("refresh", tui_state["refresh"] + 1),
                "emitRuntimeEvent": lambda event, data: events.append((event, data)),
                "getExtensionsReport": lambda: "mcp: ok",
                "requestResume": lambda next_session: resumed.append(next_session["id"]),
                "runCompaction": fake_compaction,
                "runPrompt": fake_run_prompt,
                "getStatusLineVisible": lambda: tui_state["statusline"],
                "setStatusLineVisible": lambda visible: tui_state.__setitem__("statusline", visible),
                "getTheme": lambda: tui_state["theme"],
                "setTheme": lambda theme: tui_state.__setitem__("theme", theme),
                "getMouseMode": lambda: tui_state["mouse"],
                "setMouseMode": lambda mode: tui_state.__setitem__("mouse", mode),
                "getScreenMode": lambda: tui_state["screen"],
                "setScreenMode": lambda mode: tui_state.__setitem__("screen", mode),
            }

            memory = run(core.handleSlashCommand({"command": "/memory set Prefer shell parity tests", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Replaced project memory", memory["message"])
            run(core.handleSlashCommand({"command": "/memory append Keep handlers deterministic", "session": session, "config": config, "hooks": hooks}))
            memory_show = run(core.handleSlashCommand({"command": "/memory", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Prefer shell parity tests", memory_show["message"])
            self.assertTrue(any(record["id"] == "manual-project-memory" for record in run(core.loadProjectMemoryRecords(str(workspace)))))

            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": "task-shell-monitor", "goal": "Watch shell parity"})
            run(core.saveMonitor(monitor))
            monitors = run(core.handleSlashCommand({"command": "/monitors", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Watch shell parity", monitors["message"])
            run(core.handleSlashCommand({"command": f"/monitor stop {monitor['id'][:8]}", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(run(core.loadMonitor(monitor["id"]))["status"], "stopped")
            run(core.handleSlashCommand({"command": f"/monitor resume {monitor['id']}", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(run(core.loadMonitor(monitor["id"]))["status"], "active")

            other = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="default")
            other["messages"] = [{"role": "user", "content": "resume target session"}]
            run(core.saveSession(other))
            resume_list = run(core.handleSlashCommand({"command": "/resume", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("resume target session", resume_list["message"])
            run(core.handleSlashCommand({"command": f"/resume {other['id'][:8]}", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(resumed, [other["id"]])

            compact = run(core.handleSlashCommand({"command": "/compact", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Checkpoint not created", compact["message"])
            self.assertEqual(compaction_calls[0]["trigger"], "manual")
            self.assertTrue(any(event == "compaction.started" for event, _ in events))
            self.assertTrue(any(event == "compaction.skipped" for event, _ in events))

            review = run(core.handleSlashCommand({"command": "/review providers", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(review["message"], "review queued")
            self.assertEqual(prompts[0]["agentName"], "reviewer")
            self.assertIn("providers", prompts[0]["prompt"])

            extensions = run(core.handleSlashCommand({"command": "/extensions", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("mcp: ok", extensions["message"])
            run(core.handleSlashCommand({"command": "/statusline off", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": "/theme next", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": "/mouse toggle", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": "/screen inline", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(tui_state["statusline"], False)
            self.assertEqual(tui_state["theme"], "copper")
            self.assertEqual(tui_state["mouse"], "capture")
            self.assertEqual(tui_state["screen"], "inline")
            self.assertGreaterEqual(tui_state["persist"], 3)
            self.assertGreaterEqual(tui_state["refresh"], 2)
        finally:
            self.restore_home(root, previous)

    def test_shell_task_and_daemon_mutation_commands(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            task = {"id": "task-shell-mutate", "sessionId": session["id"], "kind": "background_command", "status": "running", "title": "Mutable shell task", "updatedAt": "2026-01-01T00:00:00Z"}
            run(core.saveTaskRecord(task))
            stopped = []
            retried = []
            daemon_actions = []
            hooks = {"info": lambda *_: None, "error": lambda *_: None}
            supervisor = {
                "readTaskOutput": lambda task_id, _tail: {"output": f"log for {task_id}"},
                "stopTask": lambda task_id: stopped.append(task_id) or True,
                "retryTask": lambda task_id: retried.append(task_id) or {"taskId": "task-shell-retry", "outputFile": str(workspace / "retry.log")},
            }
            daemon_controller = {
                "stop": lambda: daemon_actions.append("stop"),
                "restart": lambda: daemon_actions.append("restart"),
            }

            log = run(core.handleSlashCommand({"command": "/tasks log mutate", "session": session, "config": config, "hooks": hooks, "taskSupervisor": supervisor}))
            self.assertIn("log for task-shell-mutate", log["message"])
            stop = run(core.handleSlashCommand({"command": "/tasks stop task-shell", "session": session, "config": config, "hooks": hooks, "taskSupervisor": supervisor}))
            self.assertIn("Stopped task-shell-mutate", stop["message"])
            retry = run(core.handleSlashCommand({"command": "/tasks retry mutate", "session": session, "config": config, "hooks": hooks, "taskSupervisor": supervisor}))
            self.assertIn("new task     task-shell-retry", retry["message"])
            self.assertEqual(stopped, ["task-shell-mutate"])
            self.assertEqual(retried, ["task-shell-mutate"])

            stopped_daemon = run(core.handleSlashCommand({"command": "/daemon stop", "session": session, "config": config, "hooks": hooks, "daemonController": daemon_controller}))
            restarted_daemon = run(core.handleSlashCommand({"command": "/daemon restart", "session": session, "config": config, "hooks": hooks, "daemonController": daemon_controller}))
            self.assertIn("Daemon stopped", stopped_daemon["message"])
            self.assertIn("Daemon restarted", restarted_daemon["message"])
            self.assertEqual(daemon_actions, ["stop", "restart"])
        finally:
            self.restore_home(root, previous)

    def test_platform_detection_tmux_ide_chrome_and_doctor(self):
        root, home, workspace, previous = self.temp_context()
        try:
            calls = []

            def which(command):
                return {"tmux": "/usr/bin/tmux", "code": "/usr/local/bin/code", "google-chrome": "/usr/bin/google-chrome"}.get(command)

            def exec_text(command, args):
                calls.append(("execText", command, tuple(args)))
                return "tmux 3.4\n" if args == ["-V"] else ""

            def exec_cmd(command, args, cwd=None):
                calls.append(("exec", command, tuple(args), cwd))
                return ""

            def spawn_detached(command, args, cwd=None):
                calls.append(("spawn", command, tuple(args), cwd))
                return 4321

            runner = {"which": which, "execText": exec_text, "exec": exec_cmd, "spawnDetached": spawn_detached}
            tmux = run(core.detectTmuxAvailability({"runner": runner}))
            self.assertTrue(tmux["available"])
            self.assertEqual(tmux["version"], "tmux 3.4")

            handoff = run(
                core.maybeExecIntoTmux(
                    {
                        "options": {"tmux": True},
                        "launch": {"cwd": str(workspace), "originalCwd": str(workspace), "worktreePath": str(workspace / "wt")},
                        "argv": ["--tmux", "--foo"],
                        "runner": runner,
                        "env": {},
                    }
                )
            )
            self.assertTrue(handoff["handled"])
            self.assertEqual(handoff["event"]["event"], "platform.tmux_attached")
            self.assertTrue(any(call[0] == "exec" and call[2][0] == "new-session" for call in calls))
            self.assertEqual(core.resolveTmuxLaunchContextFromEnv({"TESS_LAUNCH_CWD": "/w", "TESS_LAUNCH_ORIGINAL_CWD": "/o", "TESS_LAUNCH_WORKTREE_PATH": "/wt", "TESS_TMUX_SESSION_NAME": "s"})["tmuxSessionName"], "s")
            self.assertIn("requires `--worktree`", run(core.maybeExecIntoTmux({"options": {"tmux": True}, "launch": {"cwd": str(workspace)}, "runner": runner, "env": {}}))["error"])

            ide_targets = run(core.detectIdeTargets({"runner": runner, "env": {}}))
            self.assertEqual(ide_targets[0]["id"], "vscode")
            ide_connect = run(core.maybeAutoConnectIde(str(workspace), {"runner": runner, "env": {}}))
            self.assertEqual(ide_connect["status"], "connected")
            self.assertTrue(any(call[0] == "spawn" and call[2] == ("-r", str(workspace)) for call in calls))
            env_ide = run(core.detectIdeTargets({"runner": {"which": lambda _command: None}, "env": {"TERM_PROGRAM": "vscode", "CURSOR_TRACE_ID": "1"}}))
            self.assertEqual(env_ide[0]["id"], "cursor")
            self.assertEqual(run(core.maybeAutoConnectIde(str(workspace), {"runner": {"which": lambda _command: None}, "env": {}}))["status"], "skipped")

            chrome = run(core.detectChromeCompanionStatus({"runner": runner, "platform": "linux"}))
            self.assertTrue(chrome["supported"])
            self.assertEqual(chrome["browserBinary"], "/usr/bin/google-chrome")
            chrome_server = run(core.resolveChromeCompanionServer({"runner": runner, "platform": "linux"}))
            self.assertTrue(chrome_server["available"])
            self.assertTrue(chrome_server["enabled"])
            self.assertEqual(chrome_server["server"]["args"][:2], ["-m", "agent_core.chrome_companion"])
            rpc_status = run(core.handleChromeCompanionRpcPayload(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "chrome_status"}})))
            self.assertEqual(rpc_status["id"], 7)
            self.assertEqual(rpc_status["result"]["content"][0]["type"], "text")
            initialize_payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
            framed = b"Content-Length: " + str(len(initialize_payload)).encode() + b"\r\n\r\n" + initialize_payload
            mcp_out = io.BytesIO()
            run(core.runChromeCompanionMcpServer(io.BytesIO(framed), mcp_out))
            self.assertIn(b"tess-chrome-companion", mcp_out.getvalue())
            native_payload = json.dumps({"type": "ping"}).encode()
            native_out = io.BytesIO()
            run(core.runChromeNativeHost(io.BytesIO(len(native_payload).to_bytes(4, "little") + native_payload), native_out))
            native_raw = native_out.getvalue()
            native_length = int.from_bytes(native_raw[:4], "little")
            self.assertEqual(json.loads(native_raw[4 : 4 + native_length])["type"], "pong")

            install = run(core.inspectInstallState({"runner": runner}))
            self.assertTrue(install["installed"])
            self.assertEqual(install["mode"], "repo-only")
            self.assertEqual(run(core.runRepoInstall({"runner": runner}))["steps"][0]["status"], "skipped")
            self.assertEqual(run(core.runRepoUpdate({"runner": runner}))["upstreamStatus"], "skipped")
            with self.assertRaises(RuntimeError):
                run(core.runRepoInstall({"runner": runner, "target": "nightly"}))

            fake_repo = workspace / "platform-repo"
            (fake_repo / "agent_core").mkdir(parents=True)
            (fake_repo / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
            git_calls = []
            git_state = {"head": "aaa", "dirty": "", "has_upstream": True}

            def git_which(command):
                return "/usr/bin/git" if command == "git" else None

            def git_exec(command, args, cwd=None):
                git_calls.append((command, tuple(args), cwd))
                if args == ["rev-parse", "--show-toplevel"]:
                    return str(fake_repo)
                if args == ["status", "--porcelain"]:
                    return git_state["dirty"]
                if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
                    if not git_state["has_upstream"]:
                        raise RuntimeError("no upstream")
                    return "origin/main\n"
                if args == ["rev-parse", "HEAD"]:
                    return f"{git_state['head']}\n"
                if args == ["pull", "--ff-only"]:
                    git_state["head"] = "bbb"
                    return "updated\n"
                return ""

            update = run(core.runRepoUpdate({"runner": {"which": git_which, "exec": git_exec}, "repoRoot": str(fake_repo)}))
            self.assertEqual(update["upstreamStatus"], "updated")
            self.assertEqual(update["steps"][0]["step"], "git_pull")
            self.assertTrue(any(call[1] == ("pull", "--ff-only") for call in git_calls))
            self.assertEqual(update["installStateAfter"]["repoVersion"], "1.2.3")

            git_state["head"] = "bbb"
            git_calls.clear()
            up_to_date = run(core.runRepoUpdate({"runner": {"which": git_which, "exec": git_exec}, "repoRoot": str(fake_repo)}))
            self.assertEqual(up_to_date["upstreamStatus"], "up-to-date")
            self.assertTrue(any(call[1] == ("pull", "--ff-only") for call in git_calls))

            git_state["dirty"] = " M file.py\n"
            git_calls.clear()
            dirty = run(core.runRepoUpdate({"runner": {"which": git_which, "exec": git_exec}, "repoRoot": str(fake_repo)}))
            self.assertEqual(dirty["upstreamStatus"], "skipped")
            self.assertIn("dirty", dirty["upstreamDetail"])
            self.assertFalse(any(call[1] == ("pull", "--ff-only") for call in git_calls))

            report = run(core.collectDoctorReport(str(workspace), {"runner": runner, "platform": "linux"}))
            self.assertTrue(report["healthy"])
            check_ids = {check["id"] for check in report["checks"]}
            self.assertTrue({"auth", "package", "tmux", "ide", "chrome", "daemon", "git", "workspace", "extensions"}.issubset(check_ids))
            self.assertEqual(next(check for check in report["checks"] if check["id"] == "tmux")["status"], "pass")
            self.assertEqual(next(check for check in report["checks"] if check["id"] == "daemon")["status"], "warn")
            run(core.saveDaemonState({"phase": "running", "pid": os.getpid(), "socketPath": str(workspace / "daemon.sock"), "supervisorId": "sup", "epoch": 1, "updatedAt": "2026-01-01T00:00:00Z"}))
            daemon_report = run(core.collectDoctorReport(str(workspace), {"runner": runner, "platform": "linux"}))
            self.assertEqual(next(check for check in daemon_report["checks"] if check["id"] == "daemon")["status"], "pass")
        finally:
            self.restore_home(root, previous)

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

    def test_compaction_model_client_timeout_uses_provider_failure(self):
        import agent_core.compaction_models as compaction_models

        async def slow_request():
            await asyncio.sleep(0.05)
            return {}

        with self.assertRaises(core.ProviderRequestError) as raised:
            run(compaction_models._run_compaction_request_with_timeout(provider="openai", providerLabel="OpenAI", attempt=1, timeoutMs=1, run=slow_request))
        self.assertEqual(raised.exception.record["category"], "timeout")
        self.assertTrue(raised.exception.record["retryable"])
        self.assertEqual(raised.exception.record["phase"], "response_complete")

    def test_compaction_checkpoint_v2_artifacts_memory_and_context_bridge(self):
        root, home, workspace, previous = self.temp_context()

        class FakeCompactionClient:
            def __init__(self):
                self.calls = []

            async def complete(self, args):
                self.calls.append(args)
                if len(self.calls) == 1:
                    payload = {
                        "shouldCheckpoint": True,
                        "quality": {"classification": "rich", "reason": "captures current work"},
                        "workingContext": self.working_context("Run full Slice 4 compaction", "Persist checkpoint artifacts"),
                    }
                else:
                    payload = {"verdict": "accept", "confidence": "high", "reason": "checkpoint preserves current state", "issues": [], "notes": ["verified"]}
                return {"text": json.dumps(payload), "usage": {"input_tokens": 11, "output_tokens": 7}, "model": args["model"]}

            def working_context(self, goal, next_step):
                return {
                    "project": {
                        "description": "Python port of agent-core",
                        "facts": {"primaryLanguage": "Python", "languages": ["Python"], "ecosystem": "unittest", "packageManager": None, "evidence": ["tests/test_agent_core.py"]},
                        "keyFiles": [{"path": "agent_core/compaction.py", "role": "modified", "note": "checkpoint-v2 pipeline"}],
                    },
                    "goals": {"active": [goal], "recentRequests": ["Slice 4 compaction"], "constraints": ["Perform exactly like TypeScript"], "preferences": ["Detailed commits"]},
                    "currentState": {"completed": ["Committed and pushed previous slice"], "inProgress": ["Implementing checkpoint-v2 compaction"], "blocked": []},
                    "decisions": ["Use fixed Codex Spark compaction model selection"],
                    "discoveries": ["Python compaction scaffold was placeholder-only"],
                    "activity": {"commands": ["python3 -m unittest discover -s tests -v"], "searches": ["grep compaction.ts"], "web": [], "failures": []},
                    "nextSteps": [next_step],
                    "criticalContext": ["Verification must accept before artifacts are persisted"],
                }

        try:
            session = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
            for index in range(34):
                role = "user" if index % 2 == 0 else "assistant"
                session["messages"].append({"role": role, "content": f"{role} message {index}"})
            session["todos"] = [{"content": "Finish compaction", "activeForm": "Implementing compaction", "status": "in_progress"}]
            client = FakeCompactionClient()

            result = run(core.runCompaction({"session": session, "config": {}, "trigger": "manual", "force": True, "modelClient": client}))

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["qualityClassification"], "rich")
            self.assertEqual(len(client.calls), 2)
            self.assertIn("Compacted transcript evidence:", client.calls[0]["prompt"])
            self.assertIn("Candidate checkpoint:", client.calls[1]["prompt"])
            self.assertTrue(os.path.exists(result["artifacts"]["summary"]))
            self.assertTrue(os.path.exists(result["artifacts"]["workingContext"]))
            working = json.loads(Path(result["artifacts"]["workingContext"]).read_text())
            self.assertEqual(working["version"], "v2")
            self.assertEqual(working["compaction"]["model"], "gpt-5.3-codex-spark")
            self.assertEqual(working["sourceRanges"][0]["start"], 0)
            self.assertGreater(working["sourceRanges"][0]["end"], 0)
            self.assertEqual(session["metadata"]["compactions"], 1)
            self.assertEqual(session["metadata"]["usage"]["inputTokens"], 22)
            prompt_context = run(core.buildCompactionSystemPromptContext(session))
            self.assertIn("Archived working context", prompt_context)
            self.assertIn("active_goals:", prompt_context)
            memory = run(core.loadProjectMemoryRecords(str(workspace)))
            self.assertTrue(any(record["source"] == "compaction" and "Use fixed Codex Spark" in record["text"] for record in memory))

            bridged = dict(session)
            bridged["metadata"] = {"compactionState": {"compactedThroughMessageIndex": 13}}
            bridged_messages = core.buildMessagesForWorkingContext(bridged)
            self.assertEqual(bridged_messages[0]["content"], core.CONTINUATION_BRIDGE_MESSAGE)
        finally:
            self.restore_home(root, previous)

    def test_compaction_checkpoint_v2_repair_path_persists_repaired_checkpoint(self):
        root, home, workspace, previous = self.temp_context()

        def checkpoint(next_step):
            return {
                "project": {"description": "Python port", "facts": {"primaryLanguage": "Python", "languages": ["Python"], "ecosystem": "unittest", "packageManager": None, "evidence": []}, "keyFiles": []},
                "goals": {"active": ["Complete Slice 4"], "recentRequests": [], "constraints": [], "preferences": []},
                "currentState": {"completed": [], "inProgress": ["Compaction implementation"], "blocked": []},
                "decisions": [],
                "discoveries": [],
                "activity": {"commands": [], "searches": [], "web": [], "failures": []},
                "nextSteps": [next_step],
                "criticalContext": [],
            }

        class RepairClient:
            def __init__(self):
                self.calls = []

            async def complete(self, args):
                self.calls.append(args)
                if len(self.calls) == 1:
                    payload = {
                        "shouldCheckpoint": True,
                        "quality": {"classification": "usable", "reason": "needs correction"},
                        "workingContext": checkpoint("stale next step"),
                    }
                elif len(self.calls) == 2:
                    payload = {
                        "verdict": "repair",
                        "confidence": "medium",
                        "reason": "next step was stale",
                        "issues": ["stale next step"],
                        "notes": [],
                        "repairedCheckpoint": {"quality": {"classification": "usable", "reason": "repaired"}, "workingContext": checkpoint("run focused compaction tests")},
                    }
                else:
                    payload = {"verdict": "accept", "confidence": "high", "reason": "repair is accurate", "issues": [], "notes": ["repair accepted"]}
                return {"text": json.dumps(payload), "usage": {"input_tokens": 3, "output_tokens": 2}, "model": args["model"]}

        try:
            session = core.createEmptySession(cwd=str(workspace), provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
            for index in range(34):
                session["messages"].append({"role": "user" if index % 2 == 0 else "assistant", "content": f"message {index}"})
            client = RepairClient()

            result = run(core.runCompaction({"session": session, "config": {}, "trigger": "manual", "force": True, "modelClient": client}))

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(client.calls), 3)
            working = json.loads(Path(result["artifacts"]["workingContext"]).read_text())
            self.assertEqual(working["nextSteps"], ["run focused compaction tests"])
            state = json.loads(Path(result["artifacts"]["state"]).read_text())
            self.assertEqual(state["verification"]["repairCount"], 1)
        finally:
            self.restore_home(root, previous)

    def test_compaction_retrigger_policy_and_safe_tool_boundaries(self):
        session = {"messages": [{"role": "user", "content": str(index)} for index in range(110)], "metadata": {"compactionState": {"archiveMessageCount": 100, "workingContextTokenEstimate": 150_000}}}
        suppressed = core.shouldAutoCompact(session, currentInputTokens=160_000)
        self.assertFalse(suppressed["shouldCompact"])
        hard = core.shouldAutoCompact(session, currentInputTokens=181_000)
        self.assertTrue(hard["shouldCompact"])
        self.assertEqual(hard["trigger"], "auto_hard_limit")
        session["messages"].extend({"role": "user", "content": str(index)} for index in range(20))
        soft = core.shouldAutoCompact(session, currentInputTokens=160_000)
        self.assertTrue(soft["shouldCompact"])
        self.assertEqual(soft["trigger"], "auto_soft_limit")

        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "ok"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file": "a.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{\"file\":{\"filePath\":\"/tmp/a.py\",\"content\":\"x\"}}"}]},
            {"role": "user", "content": "safe restart"},
        ]
        self.assertFalse(core.isSafeWorkingSetStart(messages, 2))
        self.assertFalse(core.isSafeWorkingSetStart(messages, 3))
        self.assertTrue(core.isSafeWorkingSetStart(messages, 4))
        evidence = core.buildCompactionEvidence({"sourceMessages": messages[2:4], "sourceStart": 2, "cwd": "/tmp", "tokenBudget": 10_000})
        self.assertIn("assistant tool_use Read", evidence["transcript"])
        self.assertIn("user tool_result Read", evidence["transcript"])
        malformed = core.buildCompactionEvidence({"sourceMessages": [{"role": "assistant", "content": {"unexpected": True}}], "sourceStart": 0, "cwd": "/tmp", "tokenBudget": 10_000})
        self.assertEqual(malformed["entryCount"], 0)
        self.assertEqual(malformed["transcript"], "")
        self.assertEqual(core.parseMaybeJson('"scalar"'), '"scalar"')
        self.assertEqual(core.parseMaybeJson('{"ok":true}'), {"ok": True})

    def test_compaction_fallback_retry_matches_provider_failure_policy(self):
        session = core.createEmptySession(cwd="/tmp/workspace", provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
        source_messages = [{"role": "user", "content": "compact this"}]

        class FatalClient:
            def __init__(self):
                self.calls = 0

            async def complete(self, _args):
                self.calls += 1
                raise RuntimeError("fatal local parser error")

        fatal = FatalClient()
        with self.assertRaises(RuntimeError):
            run(core.runModelBackedCompactionSynthesisWithFallback({"session": session, "config": {}, "modelClient": fatal, "previousWorkingContext": None, "latestPlan": None, "sourceMessages": source_messages, "sourceStart": 0}))
        self.assertEqual(fatal.calls, 1)

        class ContextLimitClient:
            def __init__(self):
                self.calls = 0

            async def complete(self, args):
                self.calls += 1
                if self.calls == 1:
                    raise core.createProviderRequestError(
                        core.createProviderFailureRecord(
                            {
                                "provider": "openai",
                                "providerLabel": "OpenAI",
                                "phase": "request",
                                "category": "client_request",
                                "retryable": False,
                                "attempt": 1,
                                "hadAssistantOutput": False,
                                "hadToolUse": False,
                                "rawMessage": "input token context_length exceeded",
                            }
                        )
                    )
                payload = {
                    "shouldCheckpoint": True,
                    "quality": {"classification": "usable", "reason": "ok after smaller budget"},
                    "workingContext": {
                        "project": {"description": "Python port", "facts": None, "keyFiles": []},
                        "goals": {"active": ["Continue"], "recentRequests": [], "constraints": [], "preferences": []},
                        "currentState": {"completed": [], "inProgress": [], "blocked": []},
                        "decisions": [],
                        "discoveries": [],
                        "activity": {"commands": [], "searches": [], "web": [], "failures": []},
                        "nextSteps": [],
                        "criticalContext": [],
                    },
                }
                return {"text": json.dumps(payload), "usage": {}, "model": args["model"]}

        context_limit = ContextLimitClient()
        result = run(core.runModelBackedCompactionSynthesisWithFallback({"session": session, "config": {}, "modelClient": context_limit, "previousWorkingContext": None, "latestPlan": None, "sourceMessages": source_messages, "sourceStart": 0}))
        self.assertEqual(result["qualityClassification"], "usable")
        self.assertEqual(context_limit.calls, 2)

    def test_durable_memory_matches_compaction_record_policy(self):
        root, home, workspace, previous = self.temp_context()
        try:
            memory_file = workspace / ".tess" / "project" / "memory.md"
            memory_file.parent.mkdir(parents=True)
            memory_file.write_text("Prefer focused parity tests")
            session = core.createEmptySession(cwd=str(workspace), provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
            working = {
                "project": {"description": "Python port of agent-core", "facts": {"evidence": ["agent_core/compaction.py"]}, "keyFiles": [{"path": "agent_core/compaction.py"}]},
                "goals": {"active": ["Complete Slice 4"], "constraints": ["Match TypeScript behavior"], "preferences": ["Detailed commits"]},
                "decisions": ["Use fixed Codex Spark compaction model selection"],
                "discoveries": ["Checkpoint-v2 needs verifier repair"],
                "criticalContext": ["Compaction artifacts drive future prompt context"],
            }

            first = run(core.updateDurableMemoryFromWorkingContext(session, working))
            second = run(core.updateDurableMemoryFromWorkingContext(session, working))

            self.assertEqual(first[0]["id"], "manual-project-memory")
            self.assertEqual(first[0]["kind"], "user_preference")
            workflow = next(record for record in second if record.get("kind") == "workflow")
            expected_id = core.durableMemoryId("workflow", "Use fixed Codex Spark compaction model selection")
            self.assertEqual(workflow["id"], expected_id)
            self.assertEqual(sum(1 for record in second if record["id"] == expected_id), 1)
            retrieved = run(core.retrieveRelevantDurableMemory({"session": session, "prompt": "Codex Spark compaction"}))
            prompt = core.renderRetrievedMemoryPrompt(retrieved)
            self.assertIn("<retrieved_memory>", prompt)
            self.assertIn("[workflow | compaction | agent_core/compaction.py]", prompt)
        finally:
            self.restore_home(root, previous)

    def test_agent_prompt_auto_compacts_and_rebuilds_request_before_provider_send(self):
        import agent_core.agent as agent_module

        root, home, workspace, previous = self.temp_context()
        original_measure = agent_module.measureRequestInputTokens
        original_run_compaction = agent_module.runCompaction
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"] = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"old message {index}"} for index in range(30)]
            measure_calls = {"count": 0}
            compaction_calls = []
            provider_requests = []
            events = []

            async def fake_measure(_args):
                measure_calls["count"] += 1
                return {"inputTokens": 181_000 if measure_calls["count"] == 1 else 1_000, "source": "fake"}

            async def fake_run_compaction(args):
                compaction_calls.append(args)
                args["session"].setdefault("metadata", {})["compactionState"] = {
                    "compactedThroughMessageIndex": 11,
                    "compactedContextTokenEstimate": 200,
                    "workingContextTokenEstimate": 1_000,
                    "archiveMessageCount": len(args["session"].get("messages", [])),
                    "artifacts": {},
                }
                return {
                    "status": "completed",
                    "generation": 1,
                    "trigger": args["trigger"],
                    "compactedThroughMessageIndex": 11,
                    "compactedMessageCount": 11,
                    "recentMessageCount": len(args["session"].get("messages", [])) - 11,
                    "rawTokenEstimate": 181_000,
                    "workingContextTokenEstimate": 1_000,
                    "qualityClassification": "usable",
                    "compactionModel": {"provider": "openai", "model": "gpt-5.3-codex-spark"},
                    "verificationModel": {"provider": "openai", "model": "gpt-5.3-codex-spark"},
                    "verification": {"verdict": "accept", "confidence": "high", "reason": "ok", "issues": [], "notes": []},
                    "artifacts": {"summary": "summary.md", "state": "state.json", "workingContext": "working-context.json", "checkpoint": "checkpoint.json"},
                }

            async def send(request):
                provider_requests.append(request)
                return {"model": request["model"], "content": [{"type": "text", "text": "done"}], "usage": {}, "stopReason": "end"}

            agent_module.measureRequestInputTokens = fake_measure
            agent_module.runCompaction = fake_run_compaction
            result = run(core.runAgentPrompt(session=session, config=config, runtime={"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda event, data: events.append((event, data)), "tools": []}, prompt="continue", providerOverride={"send": send}))

            self.assertEqual(result["text"], "done")
            self.assertEqual(len(compaction_calls), 1)
            self.assertEqual(compaction_calls[0]["trigger"], "auto_hard_limit")
            self.assertEqual(measure_calls["count"], 2)
            self.assertEqual(provider_requests[0]["messages"][0]["content"], core.CONTINUATION_BRIDGE_MESSAGE)
            self.assertTrue(any(event == "compaction.completed" for event, _ in events))
            self.assertTrue(any(event == "provider.input_tokens" and data["input_tokens"] == 1_000 for event, data in events))
            self.assertEqual(session["metadata"]["runtimeRestore"]["lastCompactionMode"], "full_checkpoint_compaction")
        finally:
            agent_module.measureRequestInputTokens = original_measure
            agent_module.runCompaction = original_run_compaction
            self.restore_home(root, previous)

    def test_continuation_relief_modes_match_reference_policy(self):
        session = core.createEmptySession(cwd="/tmp/workspace", provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
        session["updatedAt"] = "2026-01-01T00:00:00Z"
        messages = []
        for index in range(18):
            tool_id = f"read-{index}"
            messages.extend(
                [
                    {"role": "user", "content": f"inspect file {index}"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": f"/tmp/workspace/file{index % 2}.py", "offset": 1}}]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": json.dumps({"file": {"filePath": f"/tmp/workspace/file{index % 2}.py", "content": "x" * 4000, "startLine": 1, "totalLines": 400, "numLines": 80}}),
                            }
                        ],
                    },
                ]
            )
        messages.append({"role": "user", "content": "continue from latest file inspection"})

        pressure = core.classifyContextPressure({"session": session, "messages": messages})
        self.assertGreaterEqual(pressure["categories"]["currentTurnToolPayloadTokens"], core.RELIEF_PRESSURE_API_MICROCOMPACT_TOKENS)
        self.assertGreater(pressure["categories"]["repeatedFileContentTokens"], 0)

        idle_relief = core.applyContextReliefModes({"session": session, "config": {"provider": "openai"}, "prompt": "continue", "messages": messages, "retrievedMemory": {"records": [], "tokenEstimate": 0}})
        self.assertIn("time_based_microcompact", idle_relief["summary"]["plannedModes"])

        session["updatedAt"] = "2999-01-01T00:00:00Z"
        relief = core.applyContextReliefModes({"session": session, "config": {"provider": "openai"}, "prompt": "continue", "messages": messages, "retrievedMemory": {"records": [], "tokenEstimate": 0}})
        summary = relief["summary"]
        self.assertIn("api_microcompact", summary["plannedModes"])
        self.assertIn("api_microcompact", summary["appliedModes"])
        self.assertGreater(summary["tokensSavedEstimate"], 0)
        prompt = core.renderContextReliefPrompt(summary)
        self.assertIn("<context_relief>", prompt)
        self.assertIn("planned_mode=api_microcompact", prompt)

    def test_continuation_session_memory_reinjection_and_partial_summary(self):
        session = core.createEmptySession(cwd="/tmp/workspace", provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
        messages = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"message {index} about /tmp/workspace/app.py and command pytest"} for index in range(44)]
        retrieved = {
            "records": [
                {"kind": "workflow", "source": "compaction", "text": "Use pytest for focused parity validation."},
                {"kind": "repo_fact", "source": "compaction", "text": "Continuation lives in agent_core/continuation.py."},
                {"kind": "constraint", "source": "compaction", "text": "Preserve active plan and todo state."},
                {"kind": "environment", "source": "manual", "text": "Workspace uses AGENT_CORE_HOME for test isolation."},
            ],
            "tokenEstimate": 650,
        }

        relief = core.applyContextReliefModes({"session": session, "config": {"provider": "openai"}, "prompt": "recover", "messages": messages, "retrievedMemory": retrieved, "reactiveAttempt": 2})
        summary = relief["summary"]
        self.assertIn("session_memory_compaction", summary["plannedModes"])
        self.assertIn("session_memory_compaction", summary["appliedModes"])
        self.assertTrue(any("durable memory:" in line for line in summary["reinjectionLines"]))
        self.assertLess(len(relief["messages"]), len(messages))

        partial = core.applyPartialCompaction(messages, 1)
        self.assertTrue(partial["applied"])
        self.assertEqual(partial["messages"][0]["role"], "user")
        self.assertIn("Partial continuation summary", partial["messages"][0]["content"])

    def test_agent_prompt_retries_context_window_failure_with_reactive_relief(self):
        import agent_core.agent as agent_module

        root, home, workspace, previous = self.temp_context()
        original_measure = agent_module.measureRequestInputTokens
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"] = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"older context {index}"} for index in range(22)]
            provider_requests = []
            events = []

            async def fake_measure(_args):
                return {"inputTokens": 1_000, "source": "fake"}

            async def send(request):
                provider_requests.append(request)
                if len(provider_requests) == 1:
                    raise core.createProviderRequestError(
                        core.createProviderFailureRecord(
                            {
                                "provider": "openai",
                                "providerLabel": "OpenAI",
                                "phase": "request_start",
                                "category": "client_request",
                                "retryable": False,
                                "attempt": 1,
                                "hadAssistantOutput": False,
                                "hadToolUse": False,
                                "rawMessage": "maximum context length exceeded",
                            }
                        )
                    )
                return {"model": request["model"], "content": [{"type": "text", "text": "recovered"}], "usage": {}, "stopReason": "end"}

            agent_module.measureRequestInputTokens = fake_measure
            result = run(core.runAgentPrompt(session=session, config=config, runtime={"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda event, data: events.append((event, data)), "tools": []}, prompt="continue", providerOverride={"send": send}))

            self.assertEqual(result["text"], "recovered")
            self.assertEqual(len(provider_requests), 2)
            self.assertIn("<context_relief>", provider_requests[1]["system"])
            self.assertTrue(any(event == "context_relief.applied" and data["reactive_attempt"] == 1 for event, data in events))
            self.assertFalse(any(event == "provider.failed" for event, _ in events))
            self.assertEqual(session["metadata"]["runtimeRestore"]["lastContextReliefReactiveAttempt"], 1)
        finally:
            agent_module.measureRequestInputTokens = original_measure
            self.restore_home(root, previous)

    def test_background_task_supervisor_and_task_output(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            bash = next(tool for tool in core.createBuiltinTools() if tool.name == "Bash")
            foreground = run(bash.handler({"command": "printf foreground-ok"}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}))
            foreground_payload = json.loads(foreground["content"])
            self.assertEqual(foreground_payload["exitCode"], 0)
            self.assertEqual(foreground["meta"]["execution"]["kind"], "tool")
            self.assertEqual(foreground["meta"]["execution"]["status"], "completed")
            self.assertEqual(foreground["meta"]["execution"]["metadata"]["exit_code"], 0)

            script = 'print("hello from background")'
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            result = run(bash.handler({"command": command, "run_in_background": True}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}))
            start_payload = json.loads(result["content"])
            task_id = start_payload["task_id"]
            self.assertEqual(start_payload["execution"]["kind"], "task")
            self.assertEqual(start_payload["execution"]["id"], task_id)
            self.assertEqual(result["meta"]["execution"]["activities"][0]["subjectId"], task_id)
            self.assertIn(task_id, session["metadata"]["taskIds"])
            daemon_state = run(core.getTaskSupervisorDaemonState())
            self.assertEqual(daemon_state["mode"], "detached_python")
            self.assertNotEqual(daemon_state["pid"], os.getpid())
            self.assertTrue(Path(daemon_state["socketPath"]).exists())
            self.assertTrue(run(core.connectTaskSupervisorDaemonClient())["_daemonClient"])
            for _ in range(80):
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
            self.assertEqual(payload["execution"]["id"], task_id)
            self.assertGreater(payload["execution"]["output"]["bytes"], 0)
            self.assertEqual(output["meta"]["execution"]["metadata"]["output_file"], payload["output_file"])
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
            completed = next(event for event in task_events if event.get("event") == "task.completed")
            self.assertEqual(completed["data"]["task_family"], "shell")
            self.assertEqual(completed["data"]["detail_status"], "result_ready")
            self.assertEqual(completed["data"]["foreground_mode"], "background")
            self.assertTrue(completed["data"]["retrieve_only"])
            self.assertIn("result_summary", completed["data"])
        finally:
            if unsubscribe:
                unsubscribe()
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_daemon_bootstrap_snapshots_attachment_counts_and_event_replay(self):
        root, home, workspace, previous = self.temp_context()
        unsubscribe = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            client = run(core.ensureTaskSupervisorDaemon({"cwd": str(workspace)}))
            script = 'print("bootstrap replay")'
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            started = run(client["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(workspace), "title": "bootstrap replay"}))
            task_id = started["taskId"]
            for _ in range(60):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": task_id, "goal": "watch replay"})
            run(core.saveMonitor(monitor))

            bootstrap = run(client["getBootstrap"](session["id"]))
            self.assertGreaterEqual(bootstrap["taskSnapshot"]["total"], 1)
            self.assertTrue(any(task.get("id") == task_id for task in bootstrap["taskSnapshot"]["tasks"]))
            self.assertEqual(bootstrap["monitorSnapshot"]["total"], 1)
            self.assertEqual(len(bootstrap["collaborationSnapshot"]["workers"]), 1)
            self.assertEqual(bootstrap["collaborationSnapshot"]["workers"][0]["kind"], "monitor")
            self.assertEqual(bootstrap["collaborationSnapshot"]["workers"][0]["monitorId"], monitor["id"])

            persisted_events = run(core.loadDaemonEvents())
            self.assertTrue(all(isinstance(event.get("cursor"), int) for event in persisted_events))
            self.assertEqual([event["cursor"] for event in persisted_events], sorted(event["cursor"] for event in persisted_events))

            events = []
            unsubscribe = client["subscribe"](events.append, {"sessionId": session["id"], "afterCursor": 0})
            for _ in range(60):
                if any(event.get("event") == "task.snapshot" for event in events) and any(event.get("event") == "task.completed" and event.get("taskId") == task_id for event in events):
                    break
                time.sleep(0.05)
            self.assertTrue(any(event.get("event") == "task.snapshot" for event in events))
            self.assertTrue(any(event.get("event") == "monitor.snapshot" for event in events))
            self.assertTrue(any(event.get("event") == "task.completed" and event.get("taskId") == task_id for event in events))
            for _ in range(40):
                state = run(core.getTaskSupervisorDaemonState())
                if state and state.get("attachedClients") == 1:
                    break
                time.sleep(0.05)
            self.assertEqual(run(core.getTaskSupervisorDaemonState())["attachedClients"], 1)
            unsubscribe()
            unsubscribe = None
            for _ in range(40):
                state = run(core.getTaskSupervisorDaemonState())
                if state and state.get("attachedClients") == 0:
                    break
                time.sleep(0.05)
            self.assertEqual(run(core.getTaskSupervisorDaemonState())["attachedClients"], 0)
        finally:
            if unsubscribe:
                unsubscribe()
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_task_supervisor_bootstrap_projects_long_lived_task_families(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"].append({"role": "user", "content": "Coordinate the long lived work."})
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": "bootstrap-monitor-task", "goal": "watch bootstrap families"})
            session["metadata"]["monitorIds"] = [monitor["id"]]
            session["metadata"]["childAgentIds"] = ["bootstrap-agent"]
            run(core.saveSession(session))
            run(core.saveMonitor(monitor))
            run(
                core.saveChildAgent(
                    {
                        "agentId": "bootstrap-agent",
                        "sessionId": "bootstrap-child-session",
                        "parentSessionId": session["id"],
                        "taskId": "bootstrap-child-task",
                        "name": "Bootstrap child",
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "mode": "bypassPermissions",
                        "runInBackground": True,
                        "status": "backgrounded",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "lastUpdatedAt": "2026-01-01T00:00:02Z",
                        "lastSummary": "Working from child session",
                        "attachableSessionId": "bootstrap-child-session",
                        "owner": "daemon",
                        "retryable": False,
                        "stoppable": True,
                    }
                )
            )
            remote = run(core.createRemoteSession({"session": session, "description": "Bootstrap remote", "channelServers": ["local"]}))
            remote = run(core.updateRemoteSession(remote["id"], {"status": "awaiting_input", "lastEventSummary": "Remote wants input"}))
            session["metadata"]["remoteSessionId"] = remote["id"]
            run(core.saveSession(session))

            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            bootstrap = run(supervisor["getBootstrap"](session["id"]))
            tasks = {task["id"]: task for task in bootstrap["taskSnapshot"]["tasks"]}
            self.assertEqual(tasks[f"monitor:{monitor['id']}"]["kind"], "monitor")
            self.assertEqual(tasks[f"monitor:{monitor['id']}"]["taskFamily"], "monitor")
            self.assertEqual(tasks["bootstrap-child-task"]["kind"], "subagent")
            self.assertEqual(tasks["bootstrap-child-task"]["status"], "backgrounded")
            self.assertEqual(tasks["bootstrap-child-task"]["metadata"]["agent_id"], "bootstrap-agent")
            self.assertEqual(tasks[f"remote:{remote['id']}"]["kind"], "remote")
            self.assertEqual(tasks[f"remote:{remote['id']}"]["status"], "awaiting_input")
            self.assertEqual(bootstrap["taskSnapshot"]["awaitingInput"], 1)
            self.assertGreaterEqual(bootstrap["taskSnapshot"]["active"], 3)
            restored = run(core.loadSession(session["id"]))["metadata"]["runtimeRestore"]
            restored_ids = {task["id"] for task in restored["activeTasks"]}
            self.assertIn(f"monitor:{monitor['id']}", restored_ids)
            self.assertIn("bootstrap-child-task", restored_ids)
            self.assertIn(f"remote:{remote['id']}", restored_ids)
            self.assertEqual(restored["restoredTaskCount"], bootstrap["taskSnapshot"]["total"])
            collaboration = bootstrap["collaborationSnapshot"]
            self.assertEqual(len(collaboration["workers"]), 3)
            self.assertEqual(collaboration["activeCount"], 2)
            self.assertEqual(collaboration["awaitingInputCount"], 1)
            self.assertEqual(collaboration["backgroundCount"], 2)
            self.assertEqual(collaboration["remoteStatus"], "awaiting_input")
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_task_runtime_emits_restored_registered_output_delta_and_restore_metadata(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            task_id = "runtime-task-family"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("first\n", encoding="utf-8")
            session["metadata"]["taskIds"] = [task_id]
            run(core.saveSession(session))
            run(
                core.writeTaskMeta(
                    task_id,
                    {
                        "type": "background_command",
                        "taskId": task_id,
                        "id": task_id,
                        "sessionId": session["id"],
                        "command": "pytest",
                        "cwd": str(workspace),
                        "startedAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "status": "running",
                        "outputFile": output_file,
                        "title": "Runtime family",
                        "detailStatus": "running",
                        "owner": "daemon",
                    },
                )
            )
            events = []
            runtime = run(core.createTaskRuntime({"session": session, "config": config, "emit": lambda event, data: events.append((event, data)), "tickIntervalMs": 50}))
            task_events = [event for event, data in events if data.get("task_id") == task_id]
            self.assertEqual(task_events[:2], ["task.restored", "task.registered"])
            self.assertTrue(any(event == "task.snapshot" and data["tasks"][0]["task_family"] == "shell" for event, data in events))
            restored = run(core.loadSession(session["id"]))["metadata"]["runtimeRestore"]
            self.assertEqual(restored["activeTasks"][0]["id"], task_id)
            self.assertEqual(restored["restoredTaskCount"], 1)

            Path(output_file).write_text("first\nsecond\n", encoding="utf-8")
            for _ in range(40):
                if any(event == "task.output_delta" and data.get("task_id") == task_id for event, data in events):
                    break
                time.sleep(0.05)
            delta = next(data for event, data in events if event == "task.output_delta" and data.get("task_id") == task_id)
            self.assertGreater(delta["bytes_delta"], 0)

            meta = run(core.readTaskMeta(task_id))
            meta.update({"status": "completed", "finishedAt": "2026-01-01T00:00:03Z", "updatedAt": "2026-01-01T00:00:03Z"})
            run(core.writeTaskMeta(task_id, meta))
            for _ in range(40):
                if any(event == "task.completed" and data.get("task_id") == task_id for event, data in events):
                    break
                time.sleep(0.05)
            self.assertTrue(any(event == "task.completed" and data.get("task_id") == task_id for event, data in events))
        finally:
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_task_supervisor_projection_matches_reference_task_family_details(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["updatedAt"] = "2026-01-01T00:00:05Z"
            session["messages"].append({"role": "user", "content": "Build the parity slice."})
            session["messages"].append({"role": "assistant", "content": "The detached worker is synthesizing the result."})
            background_id = "projection-background"
            main_id = "projection-main"
            health_id = "projection-health"
            session["metadata"]["taskIds"] = [background_id, main_id, health_id]
            run(core.saveSession(session))
            background_output = core.taskOutputPath(background_id)
            main_output = core.taskOutputPath(main_id)
            health_output = core.taskOutputPath(health_id)
            for output_file, text in [(background_output, "old line\nlatest output line\n"), (main_output, ""), (health_output, "timeout\n")]:
                Path(output_file).parent.mkdir(parents=True, exist_ok=True)
                Path(output_file).write_text(text, encoding="utf-8")
            run(core.writeTaskMeta(background_id, {"type": "background_command", "taskId": background_id, "sessionId": session["id"], "command": "python -m tests", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:01Z", "status": "running", "outputFile": background_output, "pid": 12345, "owner": "daemon"}))
            run(core.writeTaskMeta(main_id, {"type": "main_session", "taskId": main_id, "sessionId": session["id"], "command": "continue", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:01Z", "status": "running", "outputFile": main_output, "attachableSessionId": session["id"], "owner": "daemon"}))
            run(core.writeTaskMeta(health_id, {"type": "health_check", "taskId": health_id, "sessionId": session["id"], "cwd": str(workspace), "title": "API health", "url": "http://localhost:9", "intervalMs": 5000, "timeoutMs": 1000, "backoffMultiplier": 2, "maxIntervalMs": 20000, "failureThreshold": 2, "status": "unhealthy", "createdAt": "2026-01-01T00:00:00Z", "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:02Z", "lastError": "timeout", "lastStatusCode": 503, "consecutiveFailures": 1, "outputFile": health_output}))

            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            bootstrap = run(supervisor["getBootstrap"](session["id"]))
            tasks = {task["id"]: task for task in bootstrap["taskSnapshot"]["tasks"]}
            background = tasks[background_id]
            self.assertEqual(background["status"], "backgrounded")
            self.assertEqual(background["summary"], "Background command is active. Latest output: latest output line")
            self.assertEqual(background["detailStatus"], "streaming_output")
            self.assertEqual(background["resultSummary"], "latest output line")
            self.assertEqual(background["metadata"]["output_preview"], "latest output line")
            self.assertEqual(background["metadata"]["detached_type"], "background_command")
            detached = tasks[main_id]
            self.assertEqual(detached["taskFamily"], "detached_session")
            self.assertEqual(detached["summary"], "This session is continuing in the background. Latest assistant state: The detached worker is synthesizing the result.")
            self.assertEqual(detached["detailStatus"], "continuing_session")
            self.assertEqual(detached["resultSummary"], "The detached worker is synthesizing the result.")
            self.assertFalse(detached["retrieveOnly"])
            health = tasks[health_id]
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(health["summary"], "Check unhealthy: timeout")
            self.assertFalse(health["foregroundable"])
            self.assertEqual(health["detailStatus"], "unhealthy")
            self.assertEqual(health["resultSummary"], "timeout")
            self.assertEqual(health["metadata"]["current_interval_ms"], 5000)
            restored = run(core.loadSession(session["id"]))["metadata"]["runtimeRestore"]
            self.assertEqual(restored["restoredTaskCount"], 3)
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_task_supervisor_read_output_defaults_to_reference_tail_size(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "tail-default-task"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("a" * 13000, encoding="utf-8")
            run(core.writeTaskMeta(task_id, {"type": "background_command", "taskId": task_id, "id": task_id, "sessionId": "tail-session", "command": "long-output", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "status": "completed", "outputFile": output_file}))
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            default_tail = run(supervisor["readTaskOutput"](task_id))
            self.assertTrue(default_tail["truncated"])
            self.assertEqual(len(default_tail["output"]), 12000)
            explicit_full = run(supervisor["readTaskOutput"](task_id, 0))
            self.assertFalse(explicit_full["truncated"])
            self.assertEqual(len(explicit_full["output"]), 13000)
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_task_supervisor_launch_records_backgrounded_task_and_session_metadata(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            events = []
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(0.4)')}"
            started = run(supervisor["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(workspace), "title": "background parity"}))
            task_id = started["taskId"]
            record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(record["status"], "backgrounded")
            self.assertEqual(record["taskFamily"], "shell")
            self.assertTrue(record["retrieveOnly"])
            persisted = run(core.loadSession(session["id"]))
            self.assertIn(task_id, persisted["metadata"]["taskIds"])
            start_event = next(event for event in events if event["event"] == "task.updated" and event["data"]["task_id"] == task_id)
            self.assertEqual(start_event["data"]["status"], "backgrounded")
        finally:
            if supervisor:
                run(supervisor["close"]())
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_daemon_connection_cleans_stale_state_and_socket(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.ensureBaseLayout(str(workspace)))
            socket_path = core.daemonSocketPath()
            Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
            Path(socket_path).write_text("stale", encoding="utf-8")
            run(core.saveDaemonState({"pid": 999999999, "socketPath": socket_path, "status": "running"}))
            self.assertIsNone(run(core.connectTaskSupervisorDaemonClient()))
            self.assertFalse(Path(socket_path).exists())
            self.assertIsNone(run(core.loadDaemonState()))
        finally:
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

    def test_reconcile_recovers_completed_subagent_metadata_after_restart(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "subagent-recovered-task"
            agent_id = "subagent-recovered-agent"
            child_session_id = "subagent-recovered-session"
            parent_session_id = "parent-recovered-session"
            output_file = core.taskOutputPath(task_id)
            proc = subprocess.Popen([sys.executable, "-c", ""], cwd=str(workspace), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc.wait(timeout=2)
            completed_at = "2026-01-01T00:00:03Z"
            run(
                core.saveChildAgent(
                    {
                        "agentId": agent_id,
                        "sessionId": child_session_id,
                        "parentSessionId": parent_session_id,
                        "name": "Recovered worker",
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "mode": "bypassPermissions",
                        "runInBackground": True,
                        "status": "completed",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "completedAt": completed_at,
                        "latestResultAt": completed_at,
                        "lastUpdatedAt": completed_at,
                        "completionReason": "worker_completed",
                        "resultSummary": "Recovered result",
                        "lastSummary": "Recovered result",
                        "attachableSessionId": child_session_id,
                        "owner": "daemon",
                        "retryable": False,
                        "stoppable": False,
                    }
                )
            )
            meta = {
                "type": "subagent",
                "taskId": task_id,
                "id": task_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "sessionId": child_session_id,
                "session_id": child_session_id,
                "parent_session_id": parent_session_id,
                "cwd": str(workspace),
                "pid": proc.pid,
                "startedAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "status": "running",
                "outputFile": output_file,
                "title": "Recovered worker",
                "attachableSessionId": child_session_id,
                "detailStatus": "worker_running",
                "owner": "daemon",
                "supervisorId": "previous-supervisor",
            }
            run(core.writeTaskMeta(task_id, meta))
            run(
                core.saveTaskRecord(
                    {
                        "id": task_id,
                        "sessionId": parent_session_id,
                        "sourceId": agent_id,
                        "kind": "subagent",
                        "status": "running",
                        "title": "Recovered worker",
                        "summary": "Background subagent is running under daemon supervision.",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "cwd": str(workspace),
                        "outputFile": output_file,
                        "foregroundMode": "background",
                        "taskFamily": "subagent",
                        "detailStatus": "worker_running",
                        "retrieveOnly": False,
                        "attachableSessionId": child_session_id,
                        "relatedChildSessionIds": [child_session_id],
                        "owner": "daemon",
                        "supervisorId": "previous-supervisor",
                        "metadata": {"agent_id": agent_id, "child_session_id": child_session_id, "pid": proc.pid},
                    }
                )
            )
            events = []
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["summary"], "Recovered result")
            self.assertEqual(record["detailStatus"], "worker_completed")
            self.assertEqual(record["metadata"]["recovery_state"], "completed_while_detached")
            next_meta = run(core.readTaskMeta(task_id))
            self.assertEqual(next_meta["status"], "completed")
            self.assertEqual(next_meta["completionReason"], "worker_completed")
            self.assertEqual(next_meta["resultSummary"], "Recovered result")
            child = run(core.loadChildAgent(agent_id))
            self.assertEqual(child["recoveryState"], "completed_while_detached")
            self.assertTrue(any(event["event"] == "task.completed" and event["data"]["task_id"] == task_id for event in events))
        finally:
            if supervisor:
                run(supervisor["close"]())
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_stop_task_marks_subagent_child_cancelled(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        proc = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "subagent-stop-task"
            agent_id = "subagent-stop-agent"
            child_session_id = "subagent-stop-session"
            parent_session_id = "parent-stop-session"
            output_file = core.taskOutputPath(task_id)
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(workspace)}
            if hasattr(os, "setsid"):
                kwargs["preexec_fn"] = os.setsid
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
            run(
                core.saveChildAgent(
                    {
                        "agentId": agent_id,
                        "sessionId": child_session_id,
                        "parentSessionId": parent_session_id,
                        "name": "Stopped worker",
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "mode": "bypassPermissions",
                        "runInBackground": True,
                        "status": "backgrounded",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "taskId": task_id,
                        "attachableSessionId": child_session_id,
                        "owner": "daemon",
                        "retryable": False,
                        "stoppable": True,
                    }
                )
            )
            meta = {
                "type": "subagent",
                "taskId": task_id,
                "id": task_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "sessionId": child_session_id,
                "session_id": child_session_id,
                "parent_session_id": parent_session_id,
                "cwd": str(workspace),
                "pid": proc.pid,
                "startedAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "status": "running",
                "outputFile": output_file,
                "title": "Stopped worker",
                "attachableSessionId": child_session_id,
                "detailStatus": "worker_running",
                "owner": "daemon",
            }
            run(core.writeTaskMeta(task_id, meta))
            run(
                core.saveTaskRecord(
                    {
                        "id": task_id,
                        "sessionId": parent_session_id,
                        "sourceId": agent_id,
                        "kind": "subagent",
                        "status": "running",
                        "title": "Stopped worker",
                        "summary": "Background subagent is running under daemon supervision.",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "cwd": str(workspace),
                        "outputFile": output_file,
                        "foregroundMode": "background",
                        "taskFamily": "subagent",
                        "detailStatus": "worker_running",
                        "retrieveOnly": False,
                        "attachableSessionId": child_session_id,
                        "relatedChildSessionIds": [child_session_id],
                        "owner": "daemon",
                        "metadata": {"agent_id": agent_id, "child_session_id": child_session_id, "pid": proc.pid},
                    }
                )
            )
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            self.assertTrue(run(supervisor["stopTask"](task_id)))
            child = run(core.loadChildAgent(agent_id))
            self.assertEqual(child["status"], "cancelled")
            self.assertEqual(child["completionReason"], "cancelled")
            self.assertFalse(child["stoppable"])
            self.assertTrue(child["retryable"])
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["status"], "stopped")
            self.assertEqual(meta["completionReason"], "cancelled")
            record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(record["status"], "stopped")
        finally:
            if supervisor:
                run(supervisor["close"]())
            if proc and proc.poll() is None:
                proc.terminate()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
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
            stopped_payload = json.loads(stopped["content"])
            self.assertEqual(stopped_payload["execution"]["title"], f"Stopped background task {task_id}")
            self.assertEqual(stopped["meta"]["execution"]["status"], "terminated")
            for _ in range(20):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "stopped":
                    break
                time.sleep(0.05)
            self.assertEqual(run(core.readTaskMeta(task_id))["status"], "stopped")
        finally:
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_task_monitor_and_activity_descriptors_match_reference_shape(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            task_id = "monitor-task"
            output_file = core.taskOutputPath(task_id)
            run(
                core.writeTaskMeta(
                    task_id,
                    {
                        "type": "background_command",
                        "taskId": task_id,
                        "id": task_id,
                        "sessionId": session["id"],
                        "command": "python -m server",
                        "cwd": str(workspace),
                        "startedAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "status": "running",
                        "outputFile": output_file,
                    },
                )
            )
            monitor = next(tool for tool in core.createBuiltinTools() if tool.name == "TaskMonitorStart")
            created = run(monitor.handler({"task_id": task_id, "goal": "watch server", "summary_style": "brief", "check_interval_ms": 1000}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}))
            payload = json.loads(created["content"])
            self.assertEqual(payload["task_id"], task_id)
            self.assertEqual(payload["execution"]["title"], f"Monitoring task {task_id}")
            self.assertEqual(payload["execution"]["metadata"]["summary_style"], "brief")
            self.assertIn(payload["monitor_id"], session["metadata"]["monitorIds"])

            plugin_descriptors = core.createPluginActivityDescriptors(
                [
                    {"id": "alpha", "diagnostics": [], "tools": [1, 2], "commands": [1], "mcpServers": [], "source": "local", "capabilities": {"tools": True}},
                    {"id": "beta", "diagnostics": [{"severity": "warning"}], "tools": [], "commands": [], "mcpServers": [1]},
                    {"id": "gamma", "diagnostics": [{"severity": "error"}], "tools": [], "commands": [], "mcpServers": []},
                ]
            )
            self.assertEqual(plugin_descriptors[0]["status"], "ok")
            self.assertEqual(plugin_descriptors[0]["summary"], "alpha provides 2 tools, 1 commands, and 0 MCP servers")
            self.assertEqual(plugin_descriptors[1]["status"], "warn")
            self.assertEqual(plugin_descriptors[2]["status"], "error")

            mcp_descriptors = core.createMcpActivityDescriptors(
                [
                    {"name": "stdio", "connected": True, "state": "connected", "tools": 2, "resources": 1, "transport": "stdio"},
                    {"name": "sse", "connected": False, "state": "degraded", "tools": [], "resources": [], "error": "lost"},
                    {"name": "http", "connected": False, "state": "disconnected", "tools": [], "resources": []},
                ]
            )
            self.assertEqual([item["status"] for item in mcp_descriptors], ["ok", "warn", "error"])
            self.assertEqual(mcp_descriptors[0]["summary"], "stdio is connected with 2 tools and 1 resources")
            self.assertEqual(mcp_descriptors[1]["metadata"]["error"], "lost")
        finally:
            self.restore_home(root, previous)

    def test_monitor_runtime_emits_snapshot_status_and_assistant_update(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            task_id = "monitor-runtime-task"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("starting\n", encoding="utf-8")
            run(core.writeTaskMeta(task_id, {"type": "background_command", "taskId": task_id, "id": task_id, "sessionId": session["id"], "command": "python worker.py", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "status": "running", "outputFile": output_file}))
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": task_id, "goal": "watch runtime monitor", "checkIntervalMs": 2000})
            run(core.saveMonitor(monitor))
            events = []
            runtime = run(core.createMonitorRuntime({"session": session, "config": config, "emit": lambda event, data: events.append((event, data)), "tickIntervalMs": 50}))
            self.assertEqual(runtime["getSnapshot"]()["active"], 1)
            self.assertTrue(any(event == "monitor.snapshot" for event, _ in events))
            self.assertTrue(any(event == "monitor.status_changed" and data["task_id"] == task_id for event, data in events))
            update = next(data for event, data in events if event == "monitor.assistant_update")
            self.assertEqual(update["reason"], "start")
            self.assertIn("watch runtime monitor", update["text"])
            self.assertIn("watch runtime monitor", session["messages"][-1]["content"])
            saved = run(core.loadMonitor(monitor["id"]))
            self.assertEqual(saved["lastAssistantUpdateSummary"], update["text"])
            self.assertIn(monitor["id"], session["metadata"]["monitorIds"])
        finally:
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_monitor_runtime_uses_provider_authored_update_and_usage(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        original_send = monitors_module.sendOpenAiRequest
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            config["openaiToken"] = "test-token"
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"].append({"role": "user", "content": "Please monitor the build."})
            run(core.saveSession(session))
            task_id = "monitor-provider-task"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("pytest started\n", encoding="utf-8")
            run(core.writeTaskMeta(task_id, {"type": "background_command", "taskId": task_id, "id": task_id, "sessionId": session["id"], "command": "pytest", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "status": "running", "outputFile": output_file}))
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": task_id, "goal": "watch provider monitor", "summaryStyle": "brief", "checkIntervalMs": 2000})
            run(core.saveMonitor(monitor))
            calls = {}

            async def fake_send(args):
                calls["args"] = args
                return {"content": [{"type": "text", "text": "Provider says the build monitor is live."}], "usage": {"input_tokens": 7, "output_tokens": 3, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1}}

            monitors_module.sendOpenAiRequest = fake_send
            events = []
            runtime = run(core.createMonitorRuntime({"session": session, "config": config, "emit": lambda event, data: events.append((event, data)), "tickIntervalMs": 50}))
            update = next(data for event, data in events if event == "monitor.assistant_update")
            self.assertEqual(update["text"], "Provider says the build monitor is live.")
            request = calls["args"]["request"]
            self.assertEqual(request["tools"], [])
            payload = json.loads(request["messages"][0]["content"])
            self.assertEqual(payload["reason"], "start")
            self.assertEqual(payload["goal"], "watch provider monitor")
            self.assertEqual(payload["summary_style"], "brief")
            self.assertEqual(payload["new_output_excerpt"], "pytest started")
            self.assertEqual(session["metadata"]["usage"]["inputTokens"], 7)
            self.assertEqual(session["metadata"]["usage"]["outputTokens"], 3)
            self.assertEqual(session["metadata"]["usage"]["cacheCreationInputTokens"], 2)
            self.assertEqual(session["metadata"]["usage"]["cacheReadInputTokens"], 1)
            saved_session = run(core.loadSession(session["id"]))
            self.assertEqual(saved_session["messages"][-1]["content"], "Provider says the build monitor is live.")
        finally:
            monitors_module.sendOpenAiRequest = original_send
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_health_check_threshold_backoff_and_recovery(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                config = await core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace))
                session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
                await core.saveSession(session)
                counter = workspace / "health-count.txt"
                script = textwrap.dedent(
                    f"""
                    from pathlib import Path
                    p = Path({str(counter)!r})
                    n = int(p.read_text()) if p.exists() else 0
                    p.write_text(str(n + 1))
                    print(f"health-attempt-{{n + 1}}")
                    raise SystemExit(1 if n < 2 else 0)
                    """
                )
                command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
                events = []
                supervisor = await core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)})
                try:
                    started = await supervisor["startHealthCheck"](
                        {
                            "sessionId": session["id"],
                            "cwd": str(workspace),
                            "title": "health threshold",
                            "mode": "command",
                            "command": command,
                            "intervalMs": 250,
                            "timeoutMs": 1000,
                            "failureThreshold": 3,
                            "backoffMultiplier": 2,
                            "maxIntervalMs": 1000,
                        }
                    )
                    task_id = started["taskId"]
                    health = None
                    for _ in range(120):
                        health = await core.loadHealthCheckTask(task_id)
                        if health and health.get("status") == "active" and health.get("lastSuccessAt"):
                            break
                        await asyncio.sleep(0.05)
                    output = await supervisor["readTaskOutput"](task_id, 12000)
                    task_record = await supervisor["inspectTask"](task_id)
                    persisted = await core.loadSession(session["id"])
                    await supervisor["stopTask"](task_id)
                    return {"health": health, "events": events, "output": output, "taskRecord": task_record, "persisted": persisted, "taskId": task_id}
                finally:
                    await supervisor["close"]()

            result = run(scenario())
            health = result["health"]
            task_id = result["taskId"]
            self.assertEqual(health["status"], "active")
            self.assertEqual(health["consecutiveFailures"], 0)
            self.assertEqual(health["currentIntervalMs"], 250)
            self.assertEqual(health["lastExitCode"], 0)
            self.assertIn(task_id, result["persisted"]["metadata"]["taskIds"])
            unhealthy_failures = [event["data"]["failures"] for event in result["events"] if event["event"] == "health_check.unhealthy"]
            self.assertEqual(unhealthy_failures[:2], [1, 2])
            self.assertTrue(any(event["event"] == "health_check.recovered" and event["data"]["task_id"] == task_id for event in result["events"]))
            output = result["output"]
            self.assertIn("health-attempt-1", output["output"])
            self.assertIn("health-attempt-2", output["output"])
            self.assertIn("health-attempt-3", output["output"])
            task_record = result["taskRecord"]
            self.assertEqual(task_record["kind"], "health_check")
            self.assertEqual(task_record["taskFamily"], "health_check")
            self.assertEqual(task_record["status"], "running")
        finally:
            self.restore_home(root, previous)

    def test_tool_permission_modes_for_edits_and_interactive_switch(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            write = next(tool for tool in core.createBuiltinTools() if tool.name == "Write")

            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "Deny", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}
            with self.assertRaises(PermissionError):
                run(write.handler({"file_path": str(workspace / "blocked.txt"), "content": "blocked"}, context))

            session["permissionMode"] = "plan"
            config["permissionMode"] = "plan"
            with self.assertRaises(PermissionError) as plan_ctx:
                run(write.handler({"file_path": str(workspace / "plan.txt"), "content": "blocked"}, context))
            self.assertIn("plan mode", str(plan_ctx.exception))

            session["permissionMode"] = "acceptEdits"
            config["permissionMode"] = "acceptEdits"
            run(write.handler({"file_path": str(workspace / "accepted.txt"), "content": "ok"}, context))
            self.assertEqual((workspace / "accepted.txt").read_text(), "ok")

            events = []
            persisted = {"count": 0}
            session["permissionMode"] = "default"
            config["permissionMode"] = "default"
            interactive_context = {
                **context,
                "interactive": True,
                "askQuestion": lambda _prompt, options=None: "Switch to acceptEdits",
                "outputJson": lambda event, data: events.append((event, data)),
                "persistSession": lambda: persisted.__setitem__("count", persisted["count"] + 1),
            }
            run(write.handler({"file_path": str(workspace / "switched.txt"), "content": "yes"}, interactive_context))
            self.assertEqual(session["permissionMode"], "acceptEdits")
            self.assertEqual(config["permissionMode"], "acceptEdits")
            self.assertEqual(persisted["count"], 1)
            self.assertTrue(any(event == "permission.requested" for event, _ in events))
            self.assertTrue(any(data.get("source") == "prompt-switch" for event, data in events if event == "permission.decided"))
            self.assertEqual((workspace / "switched.txt").read_text(), "yes")
        finally:
            self.restore_home(root, previous)

    def test_auto_mode_shell_policy_allows_and_denies_commands(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "auto"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="auto")
            run(core.saveAutoMode(str(workspace), {"allow": ["echo"], "deny": [sys.executable], "environment": {}}))
            bash = next(tool for tool in core.createBuiltinTools() if tool.name == "Bash")
            events = []
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "Deny", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "persistSession": lambda: None}

            allowed = run(bash.handler({"command": "echo auto-ok"}, context))
            allowed_payload = json.loads(allowed["content"])
            self.assertEqual(allowed_payload["exitCode"], 0)
            self.assertIn("auto-ok", allowed_payload["stdout"])
            self.assertTrue(any(data.get("source") == "auto-mode" and data.get("decision") == "allow" for event, data in events if event == "permission.decided"))

            with self.assertRaises(PermissionError) as deny_ctx:
                run(bash.handler({"command": f"{shlex.quote(sys.executable)} -c {shlex.quote('print(1)')}"}, context))
            self.assertIn("deny policy", str(deny_ctx.exception))
            self.assertTrue(any(data.get("source") == "auto-mode" and data.get("decision") == "deny" for event, data in events if event == "permission.decided"))

            with self.assertRaises(PermissionError) as unknown_ctx:
                run(bash.handler({"command": "uname -a"}, context))
            self.assertIn("not in the allow policy", str(unknown_ctx.exception))
        finally:
            self.restore_home(root, previous)

    def test_child_session_inherits_interactive_parent_permission_mode(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            child.setdefault("metadata", {})["parentSessionId"] = parent["id"]
            run(core.saveSession(parent))
            write = next(tool for tool in core.createBuiltinTools() if tool.name == "Write")
            persisted = {"count": 0}
            context = {"session": child, "config": config, "interactive": True, "askQuestion": lambda *_: "Deny", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: persisted.__setitem__("count", persisted["count"] + 1)}
            run(write.handler({"file_path": str(workspace / "inherited.txt"), "content": "ok"}, context))
            self.assertEqual(child["permissionMode"], "bypassPermissions")
            self.assertEqual(config["permissionMode"], "bypassPermissions")
            self.assertEqual(persisted["count"], 1)
            self.assertEqual((workspace / "inherited.txt").read_text(), "ok")
        finally:
            self.restore_home(root, previous)

    def test_agent_tool_foreground_prepares_child_session_and_record(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "acceptEdits"}, str(workspace)))
            config["agents"] = {"researcher": {"prompt": "Research only.", "model": "gpt-mini"}}
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="acceptEdits")
            agent = next(tool for tool in core.createBuiltinTools() if tool.name == "Agent")
            self.assertEqual(agent.inputSchema["required"], ["description", "prompt"])
            self.assertIn("subagent_type", agent.inputSchema["properties"])
            events = []
            persisted = {"count": 0}
            captured = {}

            async def run_subagent(args, _context):
                captured.update(args)
                return {"text": "child done", "usage": {"input_tokens": 1}, "toolCount": 2, "stopReason": "end_turn"}

            result = run(
                agent.handler(
                    {"description": "Investigate", "prompt": "Find the issue", "subagent_type": "researcher", "mode": "plan", "team_name": "alpha"},
                    {
                        "session": parent,
                        "config": config,
                        "interactive": False,
                        "askQuestion": lambda *_: "",
                        "outputJson": lambda event, data: events.append((event, data)),
                        "cwd": str(workspace),
                        "persistSession": lambda: persisted.__setitem__("count", persisted["count"] + 1),
                        "runSubagent": run_subagent,
                    },
                )
            )
            payload = json.loads(result["content"])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["content"], "child done")
            self.assertEqual(payload["agent_type"], "researcher")
            self.assertEqual(persisted["count"], 1)
            self.assertEqual(parent["metadata"]["childAgentIds"], [payload["agent_id"]])
            self.assertEqual(parent["metadata"]["childSessionIds"], [payload["session_id"]])
            self.assertEqual(captured["childSession"]["messages"], [{"role": "user", "content": "Find the issue"}])
            self.assertEqual(captured["childSession"]["permissionMode"], "acceptEdits")
            self.assertEqual(captured["childSession"]["metadata"]["depth"], 1)
            self.assertEqual(captured["childSession"]["metadata"]["parentSessionId"], parent["id"])
            self.assertEqual(captured["childConfig"]["agent"], "researcher")
            self.assertEqual(captured["childConfig"]["model"], "gpt-5-mini-2025-08-07")
            self.assertIn("# Agent Instructions\nResearch only.", captured["childConfig"]["systemPrompt"])
            child_record = run(core.loadChildAgent(payload["agent_id"]))
            self.assertEqual(child_record["status"], "completed")
            self.assertEqual(child_record["teamName"], "alpha")
            self.assertEqual(child_record["resultSummary"], "child done")
            saved_child = run(core.loadSession(payload["session_id"]))
            self.assertEqual(saved_child["metadata"]["agentId"], payload["agent_id"])
            self.assertTrue(any(event == "subagent.started" for event, _ in events))
            self.assertTrue(any(event == "subagent.completed" and data["status"] == "completed" for event, data in events))

            unknown = run(agent.handler({"description": "Nope", "prompt": "x", "subagent_type": "missing"}, {"session": parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}))
            self.assertTrue(unknown["isError"])
            self.assertIn("Unknown subagent type", unknown["content"])
        finally:
            self.restore_home(root, previous)

    def test_agent_tool_background_registers_subagent_task_metadata(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            events = []
            agent = next(tool for tool in core.createBuiltinTools() if tool.name == "Agent")
            result = run(
                agent.handler(
                    {"description": "Run async", "prompt": "Work in the background", "run_in_background": True, "name": "Async worker"},
                    {"session": parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "persistSession": lambda: None, "taskSupervisor": supervisor},
                )
            )
            payload = json.loads(result["content"])
            task_id = payload["task_id"]
            self.assertEqual(payload["status"], "running")
            self.assertEqual(parent["metadata"]["backgroundSessionIds"], [payload["session_id"]])
            self.assertEqual(parent["metadata"]["childAgentIds"], [payload["agent_id"]])
            child_record = run(core.loadChildAgent(payload["agent_id"]))
            self.assertEqual(child_record["status"], "backgrounded")
            self.assertEqual(child_record["taskId"], task_id)
            self.assertTrue(child_record["stoppable"])
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["type"], "subagent")
            self.assertEqual(meta["agent_id"], payload["agent_id"])
            self.assertEqual(meta["session_id"], payload["session_id"])
            self.assertEqual(meta["parent_session_id"], parent["id"])
            task_record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(task_record["kind"], "subagent")
            self.assertEqual(task_record["sessionId"], parent["id"])
            self.assertEqual(task_record["sourceId"], payload["agent_id"])
            self.assertEqual(task_record["attachableSessionId"], payload["session_id"])
            self.assertEqual(task_record["taskFamily"], "subagent")
            self.assertTrue(any(event == "subagent.backgrounded" and data["task_id"] == task_id for event, data in events))
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_background_subagent_worker_runs_and_finalizes_metadata(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child["messages"].append({"role": "user", "content": "finish without tools"})
            child["metadata"]["parentSessionId"] = parent["id"]
            child["metadata"]["agentId"] = "agent-worker"
            child["metadata"]["agentName"] = "Worker"
            run(core.saveSession(child))
            run(
                core.saveChildAgent(
                    {
                        "agentId": "agent-worker",
                        "sessionId": child["id"],
                        "parentSessionId": parent["id"],
                        "name": "Worker",
                        "provider": config["provider"],
                        "model": config["model"],
                        "mode": "bypassPermissions",
                        "runInBackground": True,
                        "status": "queued",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "owner": "daemon",
                        "attachableSessionId": child["id"],
                    }
                )
            )
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            started = run(supervisor["startSubagentTask"]({"parentSessionId": parent["id"], "sessionId": child["id"], "agentId": "agent-worker", "cwd": str(workspace), "label": "Worker"}))
            task_id = started["taskId"]
            for _ in range(100):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["type"], "subagent")
            self.assertEqual(meta["status"], "completed")
            self.assertEqual(meta["agent_id"], "agent-worker")
            self.assertEqual(meta["session_id"], child["id"])
            self.assertEqual(meta["completionReason"], "worker_completed")
            child_record = run(core.loadChildAgent("agent-worker"))
            self.assertEqual(child_record["status"], "completed")
            self.assertEqual(child_record["taskId"], task_id)
            self.assertFalse(child_record["stoppable"])
            self.assertEqual(child_record["completionReason"], "worker_completed")
            output = run(supervisor["readTaskOutput"](task_id, 12000))
            self.assertIn('"ok": true', output["output"])
            self.assertIn("No credential is configured", output["output"])
            task_record = None
            for _ in range(40):
                task_record = run(supervisor["inspectTask"](task_id))
                if task_record and task_record.get("status") == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(task_record["kind"], "subagent")
            self.assertEqual(task_record["status"], "completed")
            self.assertEqual(task_record["metadata"]["agent_id"], "agent-worker")
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_detached_main_session_task_runs_worker_and_retries_as_main_session(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            prompt = "finish detached main-session work"
            started = run(supervisor["startMainSessionTask"]({"sessionId": session["id"], "cwd": str(workspace), "prompt": prompt, "title": "Detached check"}))
            task_id = started["taskId"]
            for _ in range(100):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["type"], "main_session")
            self.assertEqual(meta["status"], "completed")
            self.assertEqual(meta["sessionId"], session["id"])
            self.assertEqual(meta["command"], prompt)
            output = run(supervisor["readTaskOutput"](task_id, 12000))
            self.assertIn('"ok": true', output["output"])
            self.assertIn("No credential is configured", output["output"])
            task_record = None
            for _ in range(40):
                task_record = run(supervisor["inspectTask"](task_id))
                if task_record and task_record.get("status") == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(task_record["kind"], "main_session")
            self.assertEqual(task_record["taskFamily"], "detached_session")
            self.assertFalse(task_record["retrieveOnly"])
            self.assertEqual(task_record["attachableSessionId"], session["id"])
            persisted = run(core.loadSession(session["id"]))
            self.assertIn(task_id, persisted["metadata"]["taskIds"])
            self.assertIn(session["id"], persisted["metadata"]["backgroundSessionIds"])
            self.assertTrue(any(message.get("role") == "user" and message.get("content") == prompt for message in persisted["messages"]))
            retried = run(supervisor["retryTask"](task_id))
            retry_id = retried["taskId"]
            self.assertNotEqual(retry_id, task_id)
            for _ in range(100):
                retry_meta = run(core.readTaskMeta(retry_id))
                if retry_meta and retry_meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            retry_record = run(supervisor["inspectTask"](retry_id))
            self.assertEqual(retry_record["kind"], "main_session")
            self.assertEqual(retry_record["command"], prompt)
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_worktree_tools_update_workspace_metadata_and_execution_descriptors(self):
        root, home, workspace, previous = self.temp_context()
        try:
            worktree = Path(root.name) / "workspace-slice-5"
            worktree.mkdir()
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")

            enter_snapshot = {
                "workspaceId": "workspace_slice_5",
                "workspaceName": "workspace-slice-5",
                "workspaceRoot": str(worktree),
                "cwd": str(worktree),
                "mode": "worktree",
                "gitRoot": str(workspace),
                "gitBranch": "tess/slice-5",
                "worktreePath": str(worktree),
            }
            exit_snapshot = {
                "workspaceId": "workspace",
                "workspaceName": "workspace",
                "workspaceRoot": str(workspace),
                "cwd": str(workspace),
                "mode": "repo",
                "gitRoot": str(workspace),
                "gitBranch": "main",
                "worktreePath": str(worktree),
            }

            class WorktreeController:
                async def createWorktree(self, requested_name):
                    return {
                        "worktree": {
                            "gitRoot": str(workspace),
                            "targetPath": str(worktree),
                            "branchName": "tess/slice-5",
                            "requestedName": requested_name,
                        },
                        "snapshot": enter_snapshot,
                    }

                async def exitWorktree(self):
                    return exit_snapshot

            tools = core.createBuiltinTools({"workspaceController": WorktreeController()})
            enter = next(tool for tool in tools if tool.name == "EnterWorktree")
            exit_tool = next(tool for tool in tools if tool.name == "ExitWorktree")
            cwd_updates = []

            def update_cwd(next_cwd):
                cwd_updates.append(next_cwd)
                session["cwd"] = next_cwd

            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "updateCwd": update_cwd}
            entered = run(enter.handler({"name": "slice-5"}, context))
            entered_payload = json.loads(entered["content"])
            self.assertEqual(entered_payload["cwd"], str(worktree))
            self.assertEqual(entered_payload["workspace"]["mode"], "worktree")
            self.assertEqual(entered_payload["execution"]["id"], "workspace_slice_5:enter")
            self.assertEqual(entered["meta"]["execution"]["metadata"]["worktree_path"], str(worktree))
            self.assertEqual(session["cwd"], str(worktree))
            self.assertEqual(session["metadata"]["originalCwd"], str(workspace))
            self.assertEqual(session["metadata"]["worktreePath"], str(worktree))
            self.assertEqual(session["metadata"]["workspaceMode"], "worktree")

            exited = run(exit_tool.handler({}, context))
            exited_payload = json.loads(exited["content"])
            self.assertEqual(exited_payload["cwd"], str(workspace))
            self.assertEqual(exited_payload["workspace"]["mode"], "repo")
            self.assertEqual(exited_payload["execution"]["id"], "workspace:exit")
            self.assertEqual(exited["meta"]["execution"]["title"], "Returned to workspace workspace")
            self.assertEqual(session["cwd"], str(workspace))
            self.assertEqual(session["metadata"]["worktreePath"], str(worktree))
            self.assertEqual(session["metadata"]["workspaceMode"], "repo")
            self.assertEqual(cwd_updates, [str(worktree), str(workspace)])
        finally:
            self.restore_home(root, previous)

    def test_workspace_snapshot_detects_worktree_mode_from_metadata(self):
        root, home, workspace, previous = self.temp_context()
        try:
            worktree = Path(root.name) / "workspace-slice-5"
            worktree.mkdir()
            snapshot = run(core.captureWorkspaceSnapshot(str(worktree), {"originalCwd": str(workspace), "worktreePath": str(worktree)}))
            self.assertEqual(snapshot["mode"], "worktree")
            self.assertEqual(snapshot["worktreePath"], str(worktree))
            self.assertEqual(snapshot["workspaceId"], re.sub(r"[^a-zA-Z0-9]+", "_", str(worktree)).strip("_").lower())

            exited = run(core.captureWorkspaceSnapshot(str(workspace), {"originalCwd": str(workspace), "worktreePath": str(worktree)}))
            self.assertEqual(exited["mode"], "directory")
            self.assertEqual(exited["worktreePath"], str(worktree))
        finally:
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
                core.createMcpRuntime(
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
        finally:
            if runtime:
                run(runtime["close"]())
            stop_event.set()
            server.shutdown()
            server.server_close()
            self.restore_home(root, previous)

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


if __name__ == "__main__":
    unittest.main()
