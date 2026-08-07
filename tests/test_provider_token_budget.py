from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.providers.token_budget import (
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

        minimax = resolveProviderTokenLimits({"profileName": "fireworks_minimax", "kind": "openai", "model": "accounts/fireworks/models/minimax-m2p7"})
        self.assertEqual(minimax, {"maxContextLength": 512_000, "maxOutputTokens": 512_000})

        policy = resolveCompactionPolicy({"kind": "openai", "model": "llamacpp", "maxContextLength": 32_000, "maxOutputTokens": 4_096})
        self.assertEqual(policy["compactionThresholdTokens"], 32_000)
        self.assertEqual(policy["compactionTargetTokens"], 24_000)
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

    def test_unmatched_profile_output_limit_falls_back_to_knob(self):
        from agent_core.config._knobs import configureKnobs, resetConfiguredKnobs

        try:
            limits = resolveProviderTokenLimits({"profileName": "custom", "kind": "openai", "model": "mystery-model"})
            self.assertEqual(limits["maxOutputTokens"], 16_384)
            configureKnobs({"provider": {"max_output_tokens": 32_768}})
            limits = resolveProviderTokenLimits({"profileName": "custom", "kind": "openai", "model": "mystery-model"})
            self.assertEqual(limits["maxOutputTokens"], 32_768)
        finally:
            resetConfiguredKnobs()

    def test_profile_max_output_tokens_threads_from_settings_to_wire_payloads(self):
        root, home, workspace, previous = self.temp_context()
        try:
            settings_dir = home / "settings"
            settings_dir.mkdir()
            (settings_dir / "providers.yaml").write_text(
                "provider: big-output\n"
                "providerProfiles:\n"
                "  big-output:\n"
                "    kind: openai\n"
                "    defaultModel: big-model\n"
                "    baseUrl: https://example.invalid/v1\n"
                "    maxContextLength: 1000000\n"
                "    maxOutputTokens: 128000\n"
                "    auth:\n"
                "      kind: openai_api_key\n"
                "      envVar: BIG_OUTPUT_TEST_KEY\n",
                encoding="utf-8",
            )
            (settings_dir / ".env").write_text("BIG_OUTPUT_TEST_KEY=sk-test\n", encoding="utf-8")
            config = run(core.resolveConfig({}, str(workspace)))
            profile = config["providerProfile"]
            self.assertEqual(profile["maxOutputTokens"], 128_000)

            budget = computeProviderTokenBudget({"profile": profile, "estimatedInputTokens": 50_000})
            self.assertEqual(budget["effectiveMaxOutputTokens"], 128_000)

            request = core.applyProviderTokenBudget(
                {"model": "big-model", "system": "s", "messages": [{"role": "user", "content": "hi"}], "tools": []},
                budget,
                config,
            )
            self.assertEqual(request["maxOutputTokens"], 128_000)

            self.assertEqual(testing.buildResponsesCreateParams({"request": request})["max_output_tokens"], 128_000)
            self.assertEqual(testing.buildChatCompletionsCreateParams(request)["max_tokens"], 128_000)
            self.assertEqual(testing.buildAnthropicMessagesPayload(request)["max_tokens"], 128_000)
        finally:
            self.restore_home(root, previous)
