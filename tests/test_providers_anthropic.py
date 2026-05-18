from __future__ import annotations

from typing import Any

from tests.support.agent_core_case import *


class AnthropicProviderTests(AgentCoreTestCase):
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

        nullable_tool_payload = testing.buildAnthropicMessagesPayload(
            {
                "model": "kimi-k2.6",
                "system": "s",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [
                    {
                        "name": "runtime_introspection",
                        "description": "Read runtime status.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "view": {
                                    "type": ["string", "null"],
                                    "enum": ["status", "all", None],
                                },
                                "limit": {"type": ["integer", "null"], "minimum": 1},
                            },
                            "required": [],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        )
        normalized_schema = nullable_tool_payload["tools"][0]["input_schema"]
        self.assertEqual(normalized_schema["properties"]["view"]["type"], "string")
        self.assertEqual(normalized_schema["properties"]["view"]["enum"], ["status", "all"])
        self.assertEqual(normalized_schema["properties"]["limit"]["type"], "integer")
        self.assertEqual(normalized_schema["additionalProperties"], False)

        multimodal_payload = testing.buildAnthropicMessagesPayload(
            {
                "model": "claude-sonnet-4-6",
                "system": "s",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {"type": "image", "mime_type": "image/jpeg", "data": "aW1n"},
                        ],
                    }
                ],
                "tools": [],
            }
        )
        image_block = multimodal_payload["messages"][0]["content"][1]
        self.assertEqual(image_block["type"], "image")
        self.assertEqual(image_block["source"], {"type": "base64", "media_type": "image/jpeg", "data": "aW1n"})
        count_payload = testing.buildAnthropicCountTokensPayload(request)
        self.assertNotIn("max_tokens", count_payload)
        self.assertNotIn("stream", count_payload)
        self.assertNotIn("output_config", count_payload)
        self.assertEqual(count_payload["thinking"], {"type": "adaptive"})
        self.assertEqual(testing.resolveAnthropicMessagesUrl("https://anthropic.example.test/v1/"), "https://anthropic.example.test/v1/messages?beta=true")
        self.assertEqual(testing.resolveAnthropicCountTokensUrl("https://anthropic.example.test/v1/"), "https://anthropic.example.test/v1/messages/count_tokens")
        self.assertEqual(testing.resolveAnthropicMessagesUrl("https://api.kimi.com/coding/"), "https://api.kimi.com/coding/v1/messages?beta=true")
        self.assertEqual(testing.resolveAnthropicCountTokensUrl("https://api.kimi.com/coding/"), "https://api.kimi.com/coding/v1/messages/count_tokens")
        api_key_headers = testing.buildAnthropicHeaders({"apiKey": "api-key", "customHeaders": {"x-test": "1"}}, {"stream": True})
        self.assertEqual(api_key_headers["accept"], "text/event-stream")
        self.assertEqual(api_key_headers["x-api-key"], "api-key")
        self.assertEqual(api_key_headers["x-test"], "1")
        self.assertNotIn("authorization", api_key_headers)
        self.assertEqual(api_key_headers["anthropic-beta"], "fine-grained-tool-streaming-2025-05-14")
        oauth_headers = testing.buildAnthropicHeaders({"authToken": "oauth-token", "customHeaders": {}})
        self.assertEqual(oauth_headers["accept"], "application/json")
        self.assertEqual(oauth_headers["authorization"], "Bearer oauth-token")
        self.assertNotIn("x-api-key", oauth_headers)
        self.assertIn("claude-code-20250219", oauth_headers["anthropic-beta"])
        self.assertIn("oauth-2025-04-20", oauth_headers["anthropic-beta"])
        kimi_headers = testing.buildAnthropicHeaders(
            {
                "apiKey": "kimi-key",
                "anthropicBaseUrl": "https://api.kimi.com/coding/",
                "customHeaders": {},
            }
        )
        self.assertIn("claude-code-20250219", kimi_headers["anthropic-beta"])
        self.assertNotIn("oauth-2025-04-20", kimi_headers["anthropic-beta"])
        self.assertEqual(kimi_headers["user-agent"], "claude-cli/2.1.2 (external, cli)")
        self.assertEqual(kimi_headers["x-api-key"], "kimi-key")

        emitted = []
        raw = "\n\n".join(
            [
                'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":10,"cache_creation_input_tokens":2,"cache_read_input_tokens":4}}}',
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
        self.assertEqual(normalized["usage"], {"input_tokens": 10, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 4, "output_tokens": 3})
        self.assertTrue(any(event == "message.assistant.partial" for event, _ in emitted))
        self.assertNotIn("stream", testing.buildAnthropicMessagesPayload({**request, "effort": None}))

        with self.assertRaises(core.ProviderRequestError) as ctx:
            testing.normalizeAnthropicSseResponse('event: error\ndata: {"type":"error","error":{"type":"rate_limit_error","message":"rate limit"}}', request)
        self.assertEqual(ctx.exception.record["category"], "rate_limit")
        self.assertTrue(ctx.exception.record["retryable"])

        with self.assertRaises(core.ProviderRequestError) as malformed_ctx:
            testing.normalizeAnthropicSseResponse('event: content_block_delta\ndata: {"type":"content_block_delta"', request, attempt=2)
        self.assertEqual(malformed_ctx.exception.record["phase"], "parse")
        self.assertEqual(malformed_ctx.exception.record["category"], "protocol")
        self.assertEqual(malformed_ctx.exception.record["attempt"], 2)
        self.assertEqual(malformed_ctx.exception.record["streamEventType"], "content_block_delta")
        self.assertFalse(malformed_ctx.exception.record["retryable"])
        self.assertIn("Failed to parse streamed provider event", malformed_ctx.exception.record["rawMessage"])

        bad_tool_raw = "\n\n".join(
            [
                'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_bad","name":"Read","input":{}}}',
                'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\\"not-object\\""}}',
                'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
            ]
        )
        with self.assertRaises(core.ProviderRequestError) as tool_ctx:
            testing.normalizeAnthropicSseResponse(bad_tool_raw, request)
        self.assertEqual(tool_ctx.exception.record["category"], "protocol")
        self.assertTrue(tool_ctx.exception.record["hadToolUse"])
        self.assertFalse(tool_ctx.exception.record["retryable"])
        self.assertIn("Unable to parse tool parameter JSON from model", tool_ctx.exception.record["rawMessage"])

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

    def test_anthropic_streaming_emits_live_partials_before_response_completes(self):
        partial_seen = threading.Event()
        response_can_finish = threading.Event()
        request_payload: dict[str, Any] = {}

        class StreamingAnthropicHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                request_payload.update(json.loads(self.rfile.read(length).decode("utf-8")))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for frame in (
                    'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":5}}}\n\n',
                    'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
                    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"early "}}\n\n',
                ):
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                response_can_finish.wait(1)
                for frame in (
                    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"done"}}\n\n',
                    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n',
                    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
                ):
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamingAnthropicHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            emitted: list[tuple[str, dict[str, Any]]] = []
            finished = threading.Event()
            result: dict[str, Any] = {}

            def emit_event(event: str, data: dict[str, Any]):
                emitted.append((event, data))
                if event == "message.assistant.partial" and data.get("text_delta") == "early ":
                    partial_seen.set()

            def run_request():
                try:
                    result["response"] = run(
                        core.sendAnthropicMessagesRequest(
                            {
                                "config": {
                                    "provider": "anthropic",
                                    "anthropicBaseUrl": f"http://127.0.0.1:{server.server_address[1]}/v1",
                                    "apiKey": "test-key",
                                    "customHeaders": {},
                                },
                                "request": {
                                    "model": "claude-sonnet-4-6",
                                    "system": "s",
                                    "messages": [{"role": "user", "content": "Hello"}],
                                    "tools": [],
                                },
                                "runtime": {
                                    "includePartialMessages": True,
                                    "streamProviderMessages": True,
                                    "emitEvent": emit_event,
                                },
                                "maxAttempts": 1,
                                "streamIdleTimeoutMs": 2000,
                            }
                        )
                    )
                finally:
                    finished.set()

            request_thread = threading.Thread(target=run_request, daemon=True)
            request_thread.start()
            self.assertTrue(partial_seen.wait(1), "expected first text delta before the SSE stream completed")
            self.assertFalse(finished.is_set(), "request should still be open after the first partial")
            response_can_finish.set()
            request_thread.join(2)
            self.assertTrue(finished.is_set())
            self.assertEqual(request_payload["stream"], True)
            self.assertEqual(result["response"]["content"], [{"type": "text", "text": "early done"}])
            self.assertTrue(any(event == "provider.stream_event" and data["stream_event"] == "content_block_delta" for event, data in emitted))
        finally:
            response_can_finish.set()
            server.shutdown()
            server.server_close()

    def test_anthropic_streaming_idle_timeout_resets_after_activity(self):
        class ActiveStreamingAnthropicHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                frames = [
                    'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":5}}}\n\n',
                    'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
                    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"one "}}\n\n',
                    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"two "}}\n\n',
                    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"three"}}\n\n',
                    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n',
                    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
                ]
                for frame in frames:
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.05)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ActiveStreamingAnthropicHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            emitted: list[tuple[str, dict[str, Any]]] = []
            response = run(
                core.sendAnthropicMessagesRequest(
                    {
                        "config": {
                            "provider": "anthropic",
                            "anthropicBaseUrl": f"http://127.0.0.1:{server.server_address[1]}/v1",
                            "apiKey": "test-key",
                            "customHeaders": {},
                        },
                        "request": {
                            "model": "claude-sonnet-4-6",
                            "system": "s",
                            "messages": [{"role": "user", "content": "Hello"}],
                            "tools": [],
                        },
                        "runtime": {
                            "includePartialMessages": True,
                            "streamProviderMessages": True,
                            "emitEvent": lambda event, data: emitted.append((event, data)),
                        },
                        "maxAttempts": 1,
                        "streamIdleTimeoutMs": 120,
                    }
                )
            )
            self.assertEqual(response["content"], [{"type": "text", "text": "one two three"}])
            partials = [data.get("text_delta") for event, data in emitted if event == "message.assistant.partial"]
            self.assertEqual(partials, ["one ", "two ", "three"])
        finally:
            server.shutdown()
            server.server_close()

    def test_anthropic_streaming_timeout_after_partial_output_is_not_retried(self):
        original_stream = anthropic_module._send_anthropic_messages_streaming_async
        calls = {"count": 0}

        class PartialAccumulator:
            had_assistant_output = True
            had_tool_use = False

        def stalled_stream(*_args, **kwargs):
            calls["count"] += 1
            on_activity = kwargs.get("on_activity")
            if callable(on_activity):
                on_activity(PartialAccumulator())
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            return future

        anthropic_module._send_anthropic_messages_streaming_async = stalled_stream
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(
                    core.sendAnthropicMessagesRequest(
                        {
                            "config": {"provider": "anthropic", "apiKey": "test-key", "customHeaders": {}},
                            "request": {
                                "model": "claude-sonnet-4-6",
                                "system": "s",
                                "messages": [{"role": "user", "content": "Hello"}],
                                "tools": [],
                            },
                            "runtime": {"includePartialMessages": True},
                            "maxAttempts": 2,
                            "streamIdleTimeoutMs": 1,
                        }
                    )
                )
            self.assertEqual(ctx.exception.record["category"], "timeout")
            self.assertTrue(ctx.exception.record["hadAssistantOutput"])
            self.assertFalse(ctx.exception.record["retryable"])
            self.assertEqual(calls["count"], 1)
        finally:
            anthropic_module._send_anthropic_messages_streaming_async = original_stream

    def test_anthropic_blocking_http_transport_abort_returns_before_socket_finishes(self):
        signal = SimpleNamespace(aborted=False)
        request_started = threading.Event()
        request_finished = threading.Event()

        class SlowAnthropicHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                request_started.set()
                signal.aborted = True
                time.sleep(0.4)
                body = json.dumps({"model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "late"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)
                request_finished.set()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowAnthropicHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(
                    core.sendAnthropicMessagesRequest(
                        {
                            "config": {"provider": "anthropic", "anthropicBaseUrl": f"http://127.0.0.1:{server.server_address[1]}/v1", "apiKey": "test-key", "customHeaders": {}},
                            "request": {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                            "runtime": {"abortSignal": signal},
                            "maxAttempts": 2,
                            "jsonResponseTimeoutMs": 1000,
                        }
                    )
                )
            self.assertTrue(request_started.is_set())
            self.assertFalse(request_finished.is_set())
            self.assertEqual(ctx.exception.record["category"], "cancelled")
            self.assertFalse(ctx.exception.record["retryable"])
            request_finished.wait(1)
        finally:
            server.shutdown()
            server.server_close()

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
