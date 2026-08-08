from __future__ import annotations

from tests.support.agent_core_case import *


class _OpenAiSdkStreamError(Exception):
    __module__ = "openai"

    def __init__(self, body: dict[str, str]):
        super().__init__(
            "An error occurred while processing your request. "
            "Please include request ID req_p42."
        )
        self.body = body
        self.code = body.get("code")
        self.type = body.get("type")


class Package42OpenAiSdkStreamErrorTests(AgentCoreTestCase):
    def test_structured_sdk_stream_errors_preserve_diagnostics_and_policy(self):
        base = {
            "provider": "openai",
            "providerLabel": "OpenAI/Codex",
            "phase": "stream_read",
            "attempt": 1,
            "hadAssistantOutput": False,
            "hadToolUse": False,
        }
        sdk_stream = core.normalizeUnknownProviderFailure(
            {
                **base,
                "error": _OpenAiSdkStreamError(
                    {
                        "message": "The response failed during processing.",
                        "type": "server_error",
                        "code": "server_error",
                    }
                ),
            }
        )
        self.assertEqual(sdk_stream.record["category"], "server")
        self.assertTrue(sdk_stream.record["retryable"])
        self.assertEqual(sdk_stream.record["code"], "server_error")
        self.assertEqual(sdk_stream.record["errorType"], "server_error")
        self.assertEqual(sdk_stream.record["streamEventType"], "error")
        self.assertIn('"message": "The response failed', sdk_stream.record["rawBody"])
        self.assertTrue(core.canRetryProviderFailure(sdk_stream, 2))

        sdk_invalid = core.normalizeUnknownProviderFailure(
            {
                **base,
                "error": _OpenAiSdkStreamError(
                    {
                        "message": "Unsupported request field.",
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                    }
                ),
            }
        )
        self.assertEqual(sdk_invalid.record["category"], "client_request")
        self.assertFalse(sdk_invalid.record["retryable"])
        self.assertFalse(core.canRetryProviderFailure(sdk_invalid, 2))

    def test_sdk_server_stream_error_recovers_on_second_bounded_attempt(self):
        original_delay = openai_module.shared.providerRetryDelayMs
        [setattr(_m, 'providerRetryDelayMs', lambda _attempt, *_a, **_kw: 0) for _m in (openai_module.shared, openai_module.chat, openai_module.responses, openai_module.recovery_attempt)]
        try:
            attempts = {"count": 0}
            events = []

            class SdkFailingStream:
                async def __aiter__(self):
                    raise _OpenAiSdkStreamError(
                        {
                            "message": "The response failed during processing.",
                            "type": "server_error",
                            "code": "server_error",
                        }
                    )
                    yield  # pragma: no cover

            async def create(_params):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return SdkFailingStream()
                return AsyncChunks(
                    [
                        {
                            "type": "response.completed",
                            "response": {
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "sdk-retry-ok",
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    ]
                )

            response = run(
                testing.sendOpenAiResponsesRequest(
                    {
                        "token": "test",
                        "mode": "responses",
                        "request": {
                            "model": "gpt-5.4",
                            "system": "s",
                            "messages": [{"role": "user", "content": "Hello"}],
                            "tools": [],
                        },
                        "runtime": {
                            "emitEvent": lambda event, data: events.append(
                                (event, data)
                            )
                        },
                        "maxAttempts": 2,
                        "clientFactory": lambda _options: SimpleNamespace(
                            responses=SimpleNamespace(create=create)
                        ),
                    }
                )
            )

            self.assertEqual(
                response["content"],
                [{"type": "text", "text": "sdk-retry-ok"}],
            )
            self.assertEqual(attempts["count"], 2)
            retry = next(
                data for event, data in events if event == "provider.retrying"
            )
            self.assertEqual(retry["category"], "server")
            self.assertEqual(retry["code"], "server_error")
            self.assertEqual(retry["streamEventType"], "error")
            self.assertIn('"type": "server_error"', retry["rawBody"])
        finally:
            [setattr(_m, 'providerRetryDelayMs', original_delay) for _m in (openai_module.shared, openai_module.chat, openai_module.responses, openai_module.recovery_attempt)]
