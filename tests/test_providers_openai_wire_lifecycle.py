from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.providers.openai.transport import (
    _acquire_openai_client,
    _create_openai_client,
)
from agent_core.providers.openai._wire import OpenAiWireClient
from agent_core.providers.turn_session import create_provider_turn_session
from agent_core.providers.watchdog import AbortSignal
from agent_core_harness.fake_providers.server import (
    openaiChatSseBody,
    openaiResponsesSseBody,
)
from tests.test_providers_openai_wire import _args, _server


class OpenAiWireAbortTests(unittest.TestCase):
    def test_abort_mid_stream_cancels_and_closes_socket(self):
        head = openaiResponsesSseBody(
            [
                {"type": "response.created", "response": {"id": "resp_abort"}},
                {
                    "type": "response.output_item.added",
                    "item": {"id": "item_1", "type": "message"},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "item_1",
                    "delta": "partial",
                },
            ]
        )
        tail = openaiResponsesSseBody(
            [{"type": "response.output_text.delta", "item_id": "item_1", "delta": "x"}]
        )
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "chunks": [
                        {"raw": head},
                        {"delayMs": 400, "raw": tail},
                        {"delayMs": 100, "raw": tail},
                        {"delayMs": 100, "raw": tail},
                    ],
                }
            ]
        }
        server, requests = _server(script)

        async def exercise():
            signal = AbortSignal()
            runtime = {"abortSignal": signal, "emitEvent": lambda *_: None}

            async def abort_soon():
                await asyncio.sleep(0.15)
                signal.abort()

            abort_task = asyncio.create_task(abort_soon())
            try:
                with self.assertRaises(core.ProviderRequestError) as ctx:
                    await core.send_openai_responses_request(
                        _args(server, runtime=runtime)
                    )
            finally:
                await abort_task
            return ctx.exception.record

        try:
            record = run(exercise())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if requests and requests[0].get("clientDisconnected"):
                    break
                time.sleep(0.02)
        finally:
            server.close()
        self.assertEqual(record["category"], "cancelled")
        self.assertTrue(record["hadAssistantOutput"])
        self.assertTrue(requests[0].get("clientDisconnected"))


class OpenAiWireClientSelectionTests(unittest.TestCase):
    def test_wire_client_is_default_without_factory(self):
        client = _create_openai_client({"token": "t", "headers": {}}, None)
        self.assertIsInstance(client, OpenAiWireClient)

    def test_sdk_transport_requires_explicit_opt_in(self):
        captured = {}

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.responses = SimpleNamespace()
                self.chat = SimpleNamespace(completions=SimpleNamespace())

        with mock.patch(
            "agent_core.providers.openai.transport.importlib.import_module",
            return_value=SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI),
        ):
            client = _create_openai_client(
                {
                    "token": "t",
                    "headers": {},
                    "config": {"openaiTransport": "sdk"},
                },
                None,
            )
        self.assertNotIsInstance(client, OpenAiWireClient)
        self.assertEqual(captured["kwargs"]["api_key"], "t")

    def test_factory_identity_uses_object_not_id(self):
        async def exercise():
            turn = create_provider_turn_session(
                logical_turn_id="wire-identity",
                session={"id": "wire-identity", "provider": "openai"},
                config={"provider": "openai"},
                runtime={},
                model="gpt-5.4",
            )

            def factory_one(_options):
                return SimpleNamespace(name="one", capabilities={})

            def factory_two(_options):
                return SimpleNamespace(name="two", capabilities={})

            args = {"token": "t", "headers": {}, "turnSession": turn}
            try:
                first, reused_first = await _acquire_openai_client(
                    {**args, "clientFactory": factory_one}, None
                )
                second, reused_second = await _acquire_openai_client(
                    {**args, "clientFactory": factory_one}, None
                )
                third, reused_third = await _acquire_openai_client(
                    {**args, "clientFactory": factory_two}, None
                )
            finally:
                await turn.close()
            return first, reused_first, second, reused_second, third, reused_third

        first, reused_first, second, reused_second, third, reused_third = run(
            exercise()
        )
        self.assertFalse(reused_first)
        self.assertTrue(reused_second)
        self.assertIs(first, second)
        self.assertFalse(reused_third)
        self.assertEqual(third.name, "two")

    def test_request_options_merge_reaches_the_wire(self):
        script = None
        server, requests = _server(script)
        try:
            run(
                core.send_openai_responses_request(
                    _args(
                        server,
                        requestOptions={
                            "extra_headers": {"x-wire-extra": "yes"},
                            "temperature": 0.25,
                            "custom_vendor_field": {"enabled": True},
                        },
                    )
                )
            )
        finally:
            server.close()
        record = requests[0]
        self.assertEqual(record["headers"].get("x-wire-extra"), "yes")
        self.assertEqual(record["body"]["temperature"], 0.25)
        self.assertEqual(record["body"]["custom_vendor_field"], {"enabled": True})
        self.assertNotIn("extra_headers", record["body"])
        self.assertNotIn("extra_body", record["body"])


class FakeProviderRuntimeWireModeTests(unittest.TestCase):
    def test_scenario_runtime_wire_mode_drives_real_socket(self):
        from agent_core_harness.fake_providers.runtime import FakeProviderRuntime

        provider_runtime = FakeProviderRuntime()
        try:
            result = run(
                provider_runtime.run(
                    {"provider": "openai", "api": "responses", "wire": True}
                )
            )
        finally:
            provider_runtime.close()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["response"]["content"], [{"type": "text", "text": "ok"}]
        )
        record = provider_runtime.requests[0]
        self.assertEqual(record["api"], "responses")
        self.assertEqual(record["path"], "/v1/responses")

    def test_scenario_runtime_wire_mode_chat_completions(self):
        from agent_core_harness.fake_providers.runtime import FakeProviderRuntime

        provider_runtime = FakeProviderRuntime()
        try:
            result = run(
                provider_runtime.run(
                    {"provider": "openai", "api": "chat_completions", "wire": True}
                )
            )
        finally:
            provider_runtime.close()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            provider_runtime.requests[0]["path"], "/v1/chat/completions"
        )


class OpenAiWireChatStreamShapeTests(unittest.TestCase):
    def test_chat_stream_with_tool_calls_over_wire(self):
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_chat_1",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"x"}'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        ]
        script = {
            "chat_completions": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiChatSseBody(chunks),
                }
            ]
        }
        server, _requests = _server(script)
        try:
            response = run(core.send_openai_chat_completions_request(_args(server)))
        finally:
            server.close()
        self.assertEqual(response["stopReason"], "tool_calls")
        self.assertEqual(
            response["content"],
            [
                {
                    "type": "tool_use",
                    "id": "call_chat_1",
                    "name": "lookup",
                    "input": {"q": "x"},
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
