from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.providers.prompt_cache import (
    anthropicExplicitCacheEnabled,
    normalizeCacheUsageFields,
    openAiPromptCacheRequestOptions,
    providerAffinityKey,
    resolvePromptCachePolicy,
)


class ProviderPromptCacheTests(AgentCoreTestCase):
    def test_prompt_cache_policy_defaults_and_overrides(self):
        self.assertEqual(
            resolvePromptCachePolicy()["enabled"],
            True,
        )
        self.assertFalse(resolvePromptCachePolicy(config={"promptCache": {"mode": "off"}})["enabled"])
        self.assertEqual(
            resolvePromptCachePolicy(
                config={"providerProfile": {"promptCache": {"ttl": "1h"}}},
                request={"promptCache": {"ttl": "5m"}},
            )["ttl"],
            "5m",
        )
        self.assertTrue(anthropicExplicitCacheEnabled({}, {"model": "claude-opus-4-7"}))
        self.assertFalse(
            anthropicExplicitCacheEnabled(
                {"anthropicBaseUrl": "https://api.kimi.com/coding/"},
                {"model": "kimi-k2.6"},
            )
        )
        self.assertTrue(
            anthropicExplicitCacheEnabled(
                {
                    "anthropicBaseUrl": "https://api.kimi.com/coding/",
                    "promptCache": {"explicit": True},
                },
                {"model": "kimi-k2.6"},
            )
        )

    def test_openai_prompt_cache_options_are_provider_specific(self):
        request = {"model": "gpt-5.5", "sessionId": "session-123"}
        config = {"providerProfileName": "owner"}
        codex_options = openAiPromptCacheRequestOptions(
            mode="codex",
            base_url="https://chatgpt.com/backend-api/codex",
            config=config,
            request=request,
            request_options={"prompt_cache_retention": "24h"},
        )
        self.assertTrue(codex_options["prompt_cache_key"].startswith("agent-core-"))
        self.assertEqual(codex_options["prompt_cache_retention"], "24h")

        xai_options = openAiPromptCacheRequestOptions(
            mode="chat_completions",
            base_url="https://api.x.ai/v1",
            config=config,
            request=request,
        )
        self.assertEqual(xai_options["extra_headers"]["x-grok-conv-id"], providerAffinityKey(provider="openai", base_url="https://api.x.ai/v1", config=config, request=request))

        disabled = openAiPromptCacheRequestOptions(
            mode="responses",
            base_url=None,
            config={"promptCache": {"enabled": False}},
            request=request,
            request_options={"temperature": 0.2},
        )
        self.assertEqual(disabled, {"temperature": 0.2})

    def test_openai_compatible_cache_usage_normalization(self):
        usage: dict[str, int] = {}
        normalizeCacheUsageFields(
            usage,
            {"prompt_tokens_details": {"cached_tokens": 120}, "prompt_cache_miss_tokens": 30},
        )
        self.assertEqual(
            usage,
            {
                "cache_read_input_tokens": 120,
                "cache_hit_input_tokens": 120,
                "cache_miss_input_tokens": 30,
            },
        )
