from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tests.support.agent_core_case import (
    AgentCoreTestCase,
    AsyncChunks,
    core,
    openai_module,
    run,
    testing,
)


class _OverloadedSdkError(Exception):
    __module__ = "openai._exceptions"

    def __init__(self) -> None:
        super().__init__("Our servers are currently overloaded. Please try again later.")
        self.status_code = 503
        self.body = {
            "error": {
                "message": "Our servers are currently overloaded.",
                "type": "service_unavailable_error",
                "code": "server_is_overloaded",
            }
        }
        self.type = "service_unavailable_error"
        self.code = "server_is_overloaded"
        self.response = SimpleNamespace(status_code=503)


def _request() -> dict[str, Any]:
    return {
        "model": "gpt-5.6-sol",
        "system": "stable",
        "messages": [{"role": "user", "content": "work"}],
        "tools": [],
    }


class OpenAiRetryPolicyTests(AgentCoreTestCase):
    def test_openai_backoff_ladder_caps_at_thirty_seconds_with_bounded_jitter(self) -> None:
        ladder = [1_000, 3_000, 5_000, 10_000, 30_000, 30_000, 30_000]
        for attempt, base in enumerate(ladder, start=1):
            delay = openai_module.shared.openai_retry_delay_ms(attempt)
            self.assertGreaterEqual(delay, base)
            self.assertLessEqual(delay, base + 119)

    def test_codex_defaults_to_six_attempts_for_output_free_overload(self) -> None:
        attempts = 0
        delay_attempts: list[int] = []
        events: list[tuple[str, dict[str, Any]]] = []

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal attempts
            attempts += 1
            return AsyncChunks(
                [
                    {
                        "type": "error",
                        "code": "server_is_overloaded",
                        "message": "Our servers are currently overloaded.",
                    }
                ]
            )

        with self.assertRaises(core.ProviderRequestError) as raised:
            run(
                testing.send_openai_responses_request(
                    token="test",
                    mode="codex",
                    request=_request(),
                    runtime={
                        "emitEvent": lambda event, data: events.append((event, data))
                    },
                    retryDelay=lambda attempt: delay_attempts.append(attempt) or 0,
                    clientFactory=lambda _options: SimpleNamespace(
                        responses=SimpleNamespace(create=create)
                    ),
                )
            )

        retries = [
            data for event, data in events if event == "provider.retrying"
        ]
        self.assertEqual(attempts, 6)
        self.assertEqual(delay_attempts, [1, 2, 3, 4, 5])
        self.assertEqual([item["next_attempt"] for item in retries], [2, 3, 4, 5, 6])
        self.assertEqual(raised.exception.record["attempt"], 6)
        self.assertEqual(raised.exception.record["generation"], 6)
        self.assertEqual(raised.exception.record["category"], "server")
        self.assertTrue(raised.exception.record["retryable"])

    def test_chat_defaults_to_six_attempts_and_honors_retry_delay_override(self) -> None:
        attempts = 0
        delay_attempts: list[int] = []
        events: list[tuple[str, dict[str, Any]]] = []

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal attempts
            attempts += 1
            raise _OverloadedSdkError()

        with self.assertRaises(core.ProviderRequestError) as raised:
            run(
                testing.send_openai_chat_completions_request(
                    token="test",
                    baseUrl="https://api.openai.com/v1",
                    request=_request(),
                    runtime={
                        "emitEvent": lambda event, data: events.append((event, data))
                    },
                    retryDelay=lambda attempt: delay_attempts.append(attempt) or 0,
                    clientFactory=lambda _options: SimpleNamespace(
                        chat=SimpleNamespace(
                            completions=SimpleNamespace(create=create)
                        )
                    ),
                )
            )

        retries = [
            data for event, data in events if event == "provider.retrying"
        ]
        self.assertEqual(attempts, 6)
        self.assertEqual(delay_attempts, [1, 2, 3, 4, 5])
        self.assertEqual([item["next_attempt"] for item in retries], [2, 3, 4, 5, 6])
        self.assertEqual(raised.exception.record["attempt"], 6)
        self.assertEqual(raised.exception.record["category"], "server")
        self.assertEqual(raised.exception.record["code"], "server_is_overloaded")
        self.assertTrue(raised.exception.record["retryable"])
