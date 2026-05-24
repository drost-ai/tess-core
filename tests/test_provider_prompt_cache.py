from __future__ import annotations

from tests.support.agent_core_case import *

import agent_core.agent_loop.context as context_module
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

    def test_prompt_cache_moves_dynamic_system_context_into_latest_user_message(self):
        session = {
            "id": "session-cache",
            "cwd": "/tmp",
            "model": "claude-opus-4-7",
            "effort": "medium",
            "systemPrompt": "stable system prompt",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {},
        }
        config = {
            "provider": "anthropic",
            "providerProfile": {"kind": "anthropic"},
            "promptCache": {"enabled": True},
            "effort": "medium",
            "sessionPersistence": False,
        }
        runtime = {"emitEvent": lambda *_args, **_kwargs: None}

        async def fake_context(_args):
            return {
                "systemContext": "<context_relief>dynamic turn state</context_relief>",
                "messages": list(session["messages"]),
                "pressure": {},
                "retrievedMemory": {"records": [], "tokenEstimate": 0},
                "relief": {},
            }

        async def fake_measure(_args):
            return {"inputTokens": 123, "source": "test"}

        async def no_compaction(_args):
            return None

        with (
            mock.patch.object(context_module, "buildContinuationContext", fake_context),
            mock.patch.object(context_module, "measureRequestInputTokens", fake_measure),
            mock.patch.object(context_module, "maybeAutoCompactSession", no_compaction),
        ):
            prepared = run(
                context_module.prepareProviderRequest(
                    {
                        "session": session,
                        "config": config,
                        "runtime": runtime,
                        "baseSystemPrompt": "stable system prompt",
                        "toolRegistry": [],
                        "activeModel": "claude-opus-4-7",
                        "prompt": "",
                    }
                )
            )

        request = prepared["request"]
        self.assertEqual(request["system"], "stable system prompt")
        self.assertIn("[Dynamic continuation context]", request["messages"][-1]["content"])
        self.assertIn("<context_relief>dynamic turn state</context_relief>", request["messages"][-1]["content"])
        self.assertTrue(request["messages"][-1]["content"].endswith("hello"))

    def test_prompt_cache_disabled_preserves_dynamic_system_context_in_system(self):
        session = {
            "id": "session-cache-off",
            "cwd": "/tmp",
            "model": "claude-opus-4-7",
            "effort": "medium",
            "systemPrompt": "stable system prompt",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {},
        }
        config = {
            "provider": "anthropic",
            "providerProfile": {"kind": "anthropic"},
            "promptCache": {"enabled": False},
            "effort": "medium",
            "sessionPersistence": False,
        }
        runtime = {"emitEvent": lambda *_args, **_kwargs: None}

        async def fake_context(_args):
            return {
                "systemContext": "<context_relief>dynamic turn state</context_relief>",
                "messages": list(session["messages"]),
                "pressure": {},
                "retrievedMemory": {"records": [], "tokenEstimate": 0},
                "relief": {},
            }

        async def fake_measure(_args):
            return {"inputTokens": 123, "source": "test"}

        async def no_compaction(_args):
            return None

        with (
            mock.patch.object(context_module, "buildContinuationContext", fake_context),
            mock.patch.object(context_module, "measureRequestInputTokens", fake_measure),
            mock.patch.object(context_module, "maybeAutoCompactSession", no_compaction),
        ):
            prepared = run(
                context_module.prepareProviderRequest(
                    {
                        "session": session,
                        "config": config,
                        "runtime": runtime,
                        "baseSystemPrompt": "stable system prompt",
                        "toolRegistry": [],
                        "activeModel": "claude-opus-4-7",
                        "prompt": "",
                    }
                )
            )

        request = prepared["request"]
        self.assertIn("<context_relief>dynamic turn state</context_relief>", request["system"])
        self.assertEqual(request["messages"][-1]["content"], "hello")
