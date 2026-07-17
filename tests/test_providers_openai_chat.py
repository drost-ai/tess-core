from __future__ import annotations

from tests.support.agent_core_case import *


class OpenAIChatProviderTests(AgentCoreTestCase):
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

        params_with_options = testing.buildChatCompletionsCreateParams(
            {"model": "deepseek-v4-pro", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
            {
                "requestOptions": {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "max",
                    "model": "ignored",
                    "messages": [],
                }
            },
        )
        self.assertEqual(params_with_options["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(params_with_options["reasoning_effort"], "max")
        self.assertEqual(params_with_options["model"], "deepseek-v4-pro")
        self.assertNotEqual(params_with_options["messages"], [])

        emitted = []
        captured_streaming_params = {}

        async def create(_params):
            captured_streaming_params["params"] = _params
            return AsyncChunks(
                [
                    {"choices": [{"delta": {"content": "Planning"}, "finish_reason": None}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "Write", "arguments": '{"path":"'}}]}, "finish_reason": None}]},
                    {
                        "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'a.txt"}'}}]}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 42, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 12}},
                    },
                ]
            )

        response = run(
            testing.sendOpenAiChatCompletionsRequest(
                {
                    "token": "test-token",
                    "baseUrl": "https://api.fireworks.ai/inference/v1",
                    "config": {"providerProfileName": "fireworks"},
                    "request": {"model": "gpt-5.4", "effort": "high", "system": "s", "messages": [{"role": "user", "content": "Write"}], "tools": []},
                    "runtime": {"includePartialMessages": True, "streamProviderMessages": True, "emitEvent": lambda event, data: emitted.append({"event": event, "data": data})},
                    "maxAttempts": 1,
                    "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
                }
            )
        )
        self.assertEqual(response["content"][0], {"type": "text", "text": "Planning"})
        self.assertEqual(response["content"][1]["input"], {"path": "a.txt"})
        self.assertEqual(response["usage"], {"input_tokens": 42, "output_tokens": 7, "cache_read_input_tokens": 12, "cache_hit_input_tokens": 12})
        self.assertIn("x-session-affinity", captured_streaming_params["params"]["extra_headers"])
        self.assertTrue(any(item["event"] == "message.assistant.partial" for item in emitted))
        self.assertTrue(any(item["event"] == "provider.request_started" and item["data"]["attempt"] == 1 for item in emitted))
        self.assertTrue(any(item["event"] == "provider.stream_event" and item["data"]["stream_event"] == "chat.completion.chunk" for item in emitted))

    def test_openai_chat_tool_call_edge_parity(self):
        async def create_edges(_params):
            return AsyncChunks(
                [
                    {"choices": [{"delta": {"reasoning_content": "Need the file."}, "finish_reason": None}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": "not-a-number", "id": "call_edge", "function": {"name": "Re", "arguments": '{"path":"a.txt"}'}}]}, "finish_reason": None}]},
                    {"choices": [{"delta": {"tool_calls": [{"function": {"name": "Read"}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 9, "completion_tokens": 3}},
                ]
            )

        response = run(
            testing.sendOpenAiChatCompletionsRequest(
                {
                    "token": "test-token",
                    "request": {"model": "gpt-5.4", "effort": "high", "system": "s", "messages": [{"role": "user", "content": "Read"}], "tools": []},
                    "runtime": {},
                    "maxAttempts": 1,
                    "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_edges))),
                }
            )
        )
        self.assertEqual(response["content"], [{"type": "tool_use", "id": "call_edge", "name": "Read", "input": {"path": "a.txt"}, "provider_metadata": {"reasoning_content": "Need the file."}}])
        self.assertEqual(response["usage"], {"input_tokens": 9, "output_tokens": 3})
        self.assertEqual(response["stopReason"], "tool_calls")

        attempts = {"count": 0}

        class ToolThenCrash:
            async def __aiter__(self):
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_once", "function": {"name": "Read", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
                raise RuntimeError("socket hang up")

        async def create_partial_tool_failure(_params):
            attempts["count"] += 1
            return ToolThenCrash()

        with self.assertRaises(core.ProviderRequestError) as ctx:
            run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "test-token",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Read"}], "tools": []},
                        "runtime": {},
                        "maxAttempts": 2,
                        "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_partial_tool_failure))),
                    }
                )
            )
        self.assertEqual(attempts["count"], 1)
        self.assertEqual(ctx.exception.record["category"], "transport")
        self.assertTrue(ctx.exception.record["retryable"])
        self.assertTrue(ctx.exception.record["hadToolUse"])

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
