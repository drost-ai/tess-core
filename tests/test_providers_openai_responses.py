from __future__ import annotations

from tests.support.agent_core_case import *


class OpenAIResponsesProviderTests(AgentCoreTestCase):
    def test_openai_response_and_chat_params_keep_image_inputs(self):
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {"type": "image", "mime_type": "image/png", "data": "aW1n"},
            ],
        }

        response_params = testing.buildResponsesCreateParams(
            {
                "isCodexBackend": False,
                "request": {"model": "gpt-5.5", "system": "s", "messages": [message], "tools": []},
                "requestOptions": {"metadata": {"route": "vision-test"}, "input": []},
            }
        )
        self.assertEqual(response_params["metadata"], {"route": "vision-test"})
        self.assertNotEqual(response_params["input"], [])
        response_content = response_params["input"][0]["content"]
        self.assertEqual(response_content[0], {"type": "input_text", "text": "What is in this image?"})
        self.assertEqual(response_content[1]["type"], "input_image")
        self.assertEqual(response_content[1]["detail"], "auto")
        self.assertEqual(response_content[1]["image_url"], "data:image/png;base64,aW1n")

        chat_params = testing.buildChatCompletionsCreateParams(
            {"model": "gpt-5.5", "system": "s", "messages": [message], "tools": []}
        )
        chat_content = chat_params["messages"][1]["content"]
        self.assertEqual(chat_content[0], {"type": "text", "text": "What is in this image?"})
        self.assertEqual(chat_content[1]["type"], "image_url")
        self.assertEqual(chat_content[1]["image_url"]["url"], "data:image/png;base64,aW1n")
        self.assertEqual(chat_content[1]["image_url"]["detail"], "auto")

        redacted_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Follow-up"},
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "data": "<image:image/png:inbox/telegram/image.png:current-turn-only>",
                },
            ],
        }
        redacted_params = testing.buildResponsesCreateParams(
            {"request": {"model": "gpt-5.5", "system": "s", "messages": [redacted_message], "tools": []}}
        )
        self.assertEqual(redacted_params["input"][0]["content"], "Follow-up")

    def test_openai_codex_transport_and_compatible_retry_edges(self):
        codex_captured = {}

        async def create_codex(params):
            codex_captured["params"] = params
            return AsyncChunks(
                [
                    {
                        "type": "response.completed",
                        "response": {
                            "output": [{"type": "message", "content": [{"type": "output_text", "text": "codex-ok"}]}],
                            "usage": {"input_tokens": 5, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 4}},
                        },
                    }
                ]
            )

        def codex_factory(options):
            codex_captured["options"] = options
            return SimpleNamespace(responses=SimpleNamespace(create=create_codex))

        codex_response = run(
            testing.sendOpenAiResponsesRequest(
                {
                    "token": "codex-token",
                    "mode": "codex",
                    "headers": {"x-codex-test": "1"},
                    "request": {
                        "model": "gpt-5.3-codex-spark",
                        "effort": "high",
                        "system": "Follow the project policy.",
                        "messages": [{"role": "user", "content": "Summarize"}],
                        "tools": [{"name": "Read", "description": "Read files", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
                    },
                    "runtime": {},
                    "maxAttempts": 1,
                    "clientFactory": codex_factory,
                }
            )
        )
        self.assertEqual(codex_response["content"], [{"type": "text", "text": "codex-ok"}])
        self.assertEqual(codex_response["usage"]["cache_read_input_tokens"], 4)
        self.assertEqual(codex_captured["options"]["apiKey"], "codex-token")
        self.assertEqual(codex_captured["options"]["baseURL"], testing.DEFAULT_CODEX_BASE_URL)
        self.assertEqual(codex_captured["options"]["defaultHeaders"], {"x-codex-test": "1"})
        self.assertFalse(codex_captured["options"]["dangerouslyAllowBrowser"])
        self.assertTrue(callable(codex_captured["options"]["fetch"]))
        with self.assertRaisesRegex(RuntimeError, "aborted"):
            run(codex_captured["options"]["fetch"]("https://example.test", {"signal": SimpleNamespace(aborted=True)}))
        self.assertEqual(codex_captured["params"]["instructions"], "You are a helpful and reliable assistant.\n\nFollow the project policy.")
        self.assertEqual(codex_captured["params"]["reasoning"], {"effort": "high"})
        self.assertEqual(codex_captured["params"]["store"], False)
        self.assertEqual(codex_captured["params"]["stream"], True)
        self.assertTrue(codex_captured["params"]["prompt_cache_key"].startswith("agent-core-"))
        self.assertNotIn("max_output_tokens", codex_captured["params"])
        self.assertEqual(codex_captured["params"]["tools"][0]["parameters"]["properties"]["path"], {"type": "string"})

        original_delay = openai_module.providerRetryDelayMs
        openai_module.providerRetryDelayMs = lambda _attempt: 0
        try:
            compatible_attempts = {"count": 0}
            compatible_captured = {}
            events = []

            async def create_compatible(params):
                compatible_attempts["count"] += 1
                compatible_captured["params"] = params
                if compatible_attempts["count"] == 1:
                    raise RuntimeError("fetch failed")
                return AsyncChunks([{"choices": [{"delta": {"content": "compatible-ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}}])

            def compatible_factory(options):
                compatible_captured["options"] = options
                return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_compatible)))

            compatible_response = run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "compatible-token",
                        "baseUrl": "https://api.deepinfra.com/v1/openai",
                        "headers": {"x-provider": "deepinfra"},
                        "request": {"model": "zai-org/GLM-5", "effort": "xhigh", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"emitEvent": lambda event, data: events.append((event, data))},
                        "maxAttempts": 2,
                        "clientFactory": compatible_factory,
                    }
                )
            )
            self.assertEqual(compatible_response["content"], [{"type": "text", "text": "compatible-ok"}])
            self.assertEqual(compatible_response["usage"], {"input_tokens": 8, "output_tokens": 2})
            self.assertEqual(compatible_attempts["count"], 2)
            self.assertEqual(compatible_captured["options"]["baseURL"], "https://api.deepinfra.com/v1/openai")
            self.assertEqual(compatible_captured["options"]["defaultHeaders"], {"x-provider": "deepinfra"})
            self.assertNotIn("reasoning_effort", compatible_captured["params"])
            self.assertNotIn("store", compatible_captured["params"])
            self.assertTrue(any(event == "provider.retrying" and data["category"] == "transport" and data["providerLabel"] == "OpenAI Chat Completions" for event, data in events))
            self.assertTrue(any(event == "provider.recovered" and data["attempts"] == 2 for event, data in events))
        finally:
            openai_module.providerRetryDelayMs = original_delay

    def test_openai_responses_error_event_retry_and_protocol_edges(self):
        original_delay = openai_module.providerRetryDelayMs
        openai_module.providerRetryDelayMs = lambda _attempt: 0
        try:
            attempts = {"count": 0}
            events = []

            async def create_rate_limit(_params):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return AsyncChunks([{"type": "error", "code": "rate_limit_exceeded", "message": "rate limit"}])
                return AsyncChunks([{"type": "response.completed", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "retry-ok"}]}]}}])

            response = run(
                testing.sendOpenAiResponsesRequest(
                    {
                        "token": "test",
                        "mode": "responses",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"emitEvent": lambda event, data: events.append((event, data))},
                        "maxAttempts": 2,
                        "clientFactory": lambda _options: SimpleNamespace(responses=SimpleNamespace(create=create_rate_limit)),
                    }
                )
            )
            self.assertEqual(response["content"], [{"type": "text", "text": "retry-ok"}])
            self.assertEqual(attempts["count"], 2)
            self.assertTrue(any(event == "provider.retrying" and data["category"] == "rate_limit" and data["streamEventType"] == "error" for event, data in events))
            self.assertTrue(any(event == "provider.recovered" and data["category"] == "rate_limit" for event, data in events))
        finally:
            openai_module.providerRetryDelayMs = original_delay

        incomplete_calls = {"count": 0}

        async def create_incomplete(_params):
            incomplete_calls["count"] += 1
            return AsyncChunks([{"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}}])

        with self.assertRaises(core.ProviderRequestError) as ctx:
            run(
                testing.sendOpenAiResponsesRequest(
                    {
                        "token": "test",
                        "mode": "responses",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {},
                        "maxAttempts": 2,
                        "clientFactory": lambda _options: SimpleNamespace(responses=SimpleNamespace(create=create_incomplete)),
                    }
                )
            )
        self.assertEqual(incomplete_calls["count"], 1)
        self.assertEqual(ctx.exception.record["category"], "protocol")
        self.assertFalse(ctx.exception.record["retryable"])
        self.assertEqual(ctx.exception.record["streamEventType"], "response.incomplete")
        self.assertEqual(ctx.exception.record["rawMessage"], "incomplete: max_output_tokens")

    def test_openai_stream_abort_after_partial_output_and_tool_use(self):
        response_signal = SimpleNamespace(aborted=False)
        response_attempts = {"count": 0}

        class ResponseStreamAbort:
            async def __aiter__(self):
                yield {"type": "response.output_text.delta", "delta": "partial"}
                response_signal.aborted = True
                await asyncio.sleep(1)
                yield {"type": "response.completed", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "partial late"}]}]}}

        async def create_response_abort(_params):
            response_attempts["count"] += 1
            return ResponseStreamAbort()

        with self.assertRaises(core.ProviderRequestError) as response_ctx:
            run(
                testing.sendOpenAiResponsesRequest(
                    {
                        "token": "test",
                        "mode": "responses",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"abortSignal": response_signal},
                        "maxAttempts": 2,
                        "streamIdleTimeoutMs": 1000,
                        "clientFactory": lambda _options: SimpleNamespace(responses=SimpleNamespace(create=create_response_abort)),
                    }
                )
            )
        self.assertEqual(response_attempts["count"], 1)
        self.assertEqual(response_ctx.exception.record["category"], "cancelled")
        self.assertFalse(response_ctx.exception.record["retryable"])
        self.assertTrue(response_ctx.exception.record["hadAssistantOutput"])
        self.assertFalse(response_ctx.exception.record["hadToolUse"])

        chat_signal = SimpleNamespace(aborted=False)
        chat_attempts = {"count": 0}

        class ChatStreamAbort:
            async def __aiter__(self):
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_abort", "function": {"name": "Read", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
                chat_signal.aborted = True
                await asyncio.sleep(1)
                yield {"choices": [{"delta": {"content": "late"}, "finish_reason": "stop"}]}

        async def create_chat_abort(_params):
            chat_attempts["count"] += 1
            return ChatStreamAbort()

        with self.assertRaises(core.ProviderRequestError) as chat_ctx:
            run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "test",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"abortSignal": chat_signal},
                        "maxAttempts": 2,
                        "streamIdleTimeoutMs": 1000,
                        "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_chat_abort))),
                    }
                )
            )
        self.assertEqual(chat_attempts["count"], 1)
        self.assertEqual(chat_ctx.exception.record["category"], "cancelled")
        self.assertFalse(chat_ctx.exception.record["retryable"])
        self.assertFalse(chat_ctx.exception.record["hadAssistantOutput"])
        self.assertTrue(chat_ctx.exception.record["hadToolUse"])

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

    def test_openai_responses_streaming_tool_edges(self):
        emitted = []

        async def create_edges(_params):
            return AsyncChunks(
                [
                    {"type": "response.output_text.delta", "delta": "Plan"},
                    {"type": "response.output_item.added", "item": {"type": "function_call", "id": "item_1", "call_id": "call_1"}},
                    {"type": "response.function_call_arguments.done", "item_id": "item_1", "name": "Read", "arguments": '{"path":"a.txt"}'},
                    {"type": "response.output_item.done", "item": {"type": "function_call", "id": "item_1", "call_id": "call_1", "name": "Read", "arguments": '{"path":"a.txt"}'}},
                    {"type": "response.output_item.added", "item": {"type": "function_call", "id": "item_2", "call_id": "call_2", "name": "Write"}},
                    {"type": "response.function_call_arguments.done", "item_id": "item_2", "name": "Write", "arguments": "{}"},
                    {"type": "response.output_item.done", "item": {"type": "function_call", "id": "item_2", "call_id": "call_2", "name": "Write", "arguments": "{}"}},
                    {
                        "type": "response.completed",
                        "response": {
                            "output": [
                                {"type": "message", "content": [{"type": "output_text", "text": "Plan done"}]},
                                {"type": "function_call", "call_id": "call_1", "name": "Read", "arguments": '{"path":"a.txt"}'},
                                {"type": "function_call", "call_id": "call_3", "name": "List", "arguments": '{"dir":"."}'},
                            ],
                            "usage": {"input_tokens": 12, "output_tokens": 4},
                        },
                    },
                ]
            )

        response = run(
            testing.sendOpenAiResponsesRequest(
                {
                    "token": "test",
                    "mode": "responses",
                    "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Use tools"}], "tools": []},
                    "runtime": {"includePartialMessages": True, "streamProviderMessages": True, "emitEvent": lambda event, data: emitted.append((event, data))},
                    "maxAttempts": 1,
                    "clientFactory": lambda _options: SimpleNamespace(responses=SimpleNamespace(create=create_edges)),
                }
            )
        )
        self.assertEqual(response["content"][0], {"type": "text", "text": "Plan done"})
        self.assertEqual(response["content"][1], {"type": "tool_use", "id": "call_1", "name": "Read", "input": {"path": "a.txt"}})
        # Package 131: a complete accumulated tool call omitted from the
        # terminal projection is never silently discarded; the deterministic
        # union anchors it after its streamed predecessor.
        self.assertEqual(response["content"][2], {"type": "tool_use", "id": "call_2", "name": "Write", "input": {}})
        self.assertEqual(response["content"][3], {"type": "tool_use", "id": "call_3", "name": "List", "input": {"dir": "."}})
        self.assertEqual([block["id"] for block in response["content"] if block["type"] == "tool_use"].count("call_1"), 1)
        self.assertEqual(response["usage"], {"input_tokens": 12, "output_tokens": 4})
        self.assertEqual(response["stopReason"], "tool_calls")
        self.assertTrue(any(event == "message.assistant.partial" and data["text_delta"] == "Plan" for event, data in emitted))
        self.assertTrue(any(event == "provider.stream_event" and data["stream_event"] == "response.completed" for event, data in emitted))

        attempts = {"count": 0}

        async def create_partial_failure(_params):
            attempts["count"] += 1
            return AsyncChunks(
                [
                    {"type": "response.output_text.delta", "delta": "partial"},
                    {"type": "response.failed", "response": {"error": {"code": "server_error", "message": "stream failed"}}},
                ]
            )

        with self.assertRaises(core.ProviderRequestError) as ctx:
            run(
                testing.sendOpenAiResponsesRequest(
                    {
                        "token": "test",
                        "mode": "responses",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {},
                        "maxAttempts": 2,
                        "clientFactory": lambda _options: SimpleNamespace(responses=SimpleNamespace(create=create_partial_failure)),
                    }
                )
            )
        self.assertEqual(attempts["count"], 1)
        self.assertEqual(ctx.exception.record["category"], "server")
        self.assertTrue(ctx.exception.record["hadAssistantOutput"])
        self.assertTrue(ctx.exception.record["retryable"])
        self.assertEqual(ctx.exception.record["streamEventType"], "response.failed")

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
