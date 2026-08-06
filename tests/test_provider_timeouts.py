from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.providers.timeouts import resolveProviderTimeoutPolicy
import agent_core.agent_loop.providers as loop_providers


class ProviderTimeoutPolicyTests(AgentCoreTestCase):
    def test_subagent_runtime_contract_supplies_provider_deadlines(self):
        policy = resolveProviderTimeoutPolicy(
            {
                "config": {"providerProfile": {}},
                "runtimeContract": {"timeoutMs": 7_200_000},
            }
        )
        self.assertEqual(
            policy,
            {
                "requestStartTimeoutMs": 7_200_000,
                "streamIdleTimeoutMs": 7_200_000,
                "jsonResponseTimeoutMs": 7_200_000,
            },
        )

    def test_explicit_provider_timeouts_override_subagent_contract(self):
        policy = resolveProviderTimeoutPolicy(
            {
                "config": {
                    "providerProfile": {
                        "requestStartTimeoutMs": 300_000,
                        "streamIdleTimeoutMs": 180_000,
                    }
                },
                "runtimeContract": {"timeoutMs": 7_200_000},
            }
        )
        self.assertEqual(policy["requestStartTimeoutMs"], 300_000)
        self.assertEqual(policy["streamIdleTimeoutMs"], 180_000)
        self.assertEqual(policy["jsonResponseTimeoutMs"], 7_200_000)

    def test_agent_loop_provider_receives_subagent_runtime_contract_deadlines(self):
        captured: dict[str, Any] = {}
        original = loop_providers.sendAnthropicMessagesRequest

        async def fake_send(*_args, **kwargs):
            captured.update(kwargs)
            return {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model": kwargs["request"]["model"],
                "stopReason": "end_turn",
            }

        loop_providers.sendAnthropicMessagesRequest = fake_send
        try:
            provider = loop_providers._create_provider(
                {
                    "provider": "anthropic",
                    "apiKey": "test-key",
                    "providerProfile": {},
                    "providerProfileName": "anthropic-oauth",
                },
                {},
                {
                    "metadata": {
                        "subagentRuntimeContract": {
                            "timeoutMs": 7_200_000,
                        }
                    }
                },
            )
            response = run(
                provider["send"](
                    {
                        "model": "claude-opus-4-8",
                        "system": "s",
                        "messages": [{"role": "user", "content": "hello"}],
                        "tools": [],
                    }
                )
            )
            self.assertEqual(response["content"][0]["text"], "ok")
            self.assertEqual(captured["requestStartTimeoutMs"], 7_200_000)
            self.assertEqual(captured["streamIdleTimeoutMs"], 7_200_000)
            self.assertEqual(captured["jsonResponseTimeoutMs"], 7_200_000)
        finally:
            loop_providers.sendAnthropicMessagesRequest = original
