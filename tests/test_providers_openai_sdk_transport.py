from __future__ import annotations

from tests.support.agent_core_case import *


class OpenAISdkTransportTests(AgentCoreTestCase):
    def test_openai_sdk_transport_fallback_without_client_factory(self):
        captured: dict[str, Any] = {}

        class FakeResponses:
            async def create(self, **params):
                captured["response_params"] = params
                return AsyncChunks(
                    [
                        {
                            "type": "response.completed",
                            "response": {
                                "output": [{"type": "message", "content": [{"type": "output_text", "text": "sdk-ok"}]}],
                                "usage": {"input_tokens": 3, "output_tokens": 1},
                            },
                        }
                    ]
                )

        class FakeChatCompletions:
            async def create(self, **params):
                captured["chat_params"] = params
                return AsyncChunks([])

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs
                self.responses = FakeResponses()
                self.chat = SimpleNamespace(completions=FakeChatCompletions())

        with mock.patch(
            "agent_core.providers.openai_parts.transport.importlib.import_module",
            return_value=SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI),
        ):
            response = run(
                testing.sendOpenAiResponsesRequest(
                    {
                        "token": "sdk-token",
                        "mode": "codex",
                        "headers": {"x-sdk": "1"},
                        "request": {
                            "model": "gpt-5.5",
                            "system": "s",
                            "messages": [{"role": "user", "content": "Hello"}],
                            "tools": [],
                        },
                        "runtime": {},
                        "maxAttempts": 1,
                    }
                )
            )

        self.assertEqual(response["content"], [{"type": "text", "text": "sdk-ok"}])
        self.assertEqual(captured["client_kwargs"]["api_key"], "sdk-token")
        self.assertEqual(captured["client_kwargs"]["base_url"], testing.DEFAULT_CODEX_BASE_URL)
        self.assertEqual(captured["client_kwargs"]["default_headers"], {"x-sdk": "1"})
        self.assertEqual(captured["response_params"]["model"], "gpt-5.5")
        self.assertTrue(captured["response_params"]["stream"])
