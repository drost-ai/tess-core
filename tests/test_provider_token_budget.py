from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.provider_token_budget import (
    computeProviderTokenBudget,
    resolveCompactionPolicy,
    resolveCompactionRequestOutputTokens,
    resolveProviderTokenLimits,
)


class ProviderTokenBudgetTests(AgentCoreTestCase):
    def test_model_family_limits_and_compaction_policy(self):
        codex = resolveProviderTokenLimits({"profileName": "codex", "kind": "openai", "apiMode": "codex", "model": "gpt-5.4"})
        self.assertEqual(codex, {"maxContextLength": 400_000, "maxOutputTokens": 128_000})

        anthropic = resolveProviderTokenLimits({"kind": "anthropic", "model": "claude-opus-4-8"})
        self.assertEqual(anthropic, {"maxContextLength": 1_000_000, "maxOutputTokens": 128_000})

        kimi = resolveProviderTokenLimits({"profileName": "kimi", "kind": "anthropic", "model": "kimi-k2.6"})
        self.assertEqual(kimi, {"maxContextLength": 262_144, "maxOutputTokens": 262_144})

        policy = resolveCompactionPolicy({"kind": "openai", "model": "llamacpp", "maxContextLength": 32_000, "maxOutputTokens": 4_096})
        self.assertEqual(policy["compactionThresholdTokens"], 32_000)
        self.assertEqual(policy["compactionOutputTokens"], 3_200)
        self.assertTrue(policy["thresholdClamped"])

    def test_runtime_output_budget_clamps_to_remaining_context(self):
        budget = computeProviderTokenBudget({"profile": {"maxContextLength": 32_000, "maxOutputTokens": 8_000}, "estimatedInputTokens": 29_000})
        self.assertEqual(budget["remainingOutputTokens"], -1_096)
        self.assertEqual(budget["effectiveMaxOutputTokens"], 0)
        self.assertTrue(budget["clamped"])
        self.assertTrue(budget["insufficient"])

        request_output = resolveCompactionRequestOutputTokens({"profile": {"maxOutputTokens": 10_000, "compactionOutputTokens": 4_000}, "sourceTokenEstimate": 80_000})
        self.assertEqual(request_output, 8_000)

    def test_provider_payloads_use_request_output_budget(self):
        request = {"model": "gpt-5.4", "maxOutputTokens": 4_321, "system": "s", "messages": [{"role": "user", "content": "hi"}], "tools": []}
        self.assertEqual(testing.buildResponsesCreateParams({"request": request})["max_output_tokens"], 4_321)
        self.assertEqual(testing.buildChatCompletionsCreateParams(request)["max_tokens"], 4_321)
        self.assertEqual(testing.buildAnthropicMessagesPayload({**request, "model": "claude-opus-4-8"})["max_tokens"], 4_321)
