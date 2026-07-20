from __future__ import annotations

from tests.support.agent_core_case import *


class CompactionModelFetchTests(AgentCoreTestCase):
    def test_fetch_proxy_and_compaction_model_defaults(self):
        self.assertEqual(core.resolveEnvironmentProxyForUrl("https://api.openai.com/v1/responses", {"HTTPS_PROXY": "https-proxy", "HTTP_PROXY": "http-proxy"}), "https-proxy")
        self.assertEqual(core.resolveEnvironmentProxyForUrl("https://api.openai.com/v1/responses", {"HTTP_PROXY": "http-proxy"}), "http-proxy")
        self.assertIsNone(core.resolveEnvironmentProxyForUrl("file:///tmp/a", {"HTTPS_PROXY": "https-proxy"}))

        session = core.createEmptySession(cwd="/tmp/workspace", provider="openai", providerProfileName="openai", model="gpt-5.4", effort="xhigh", systemPrompt="s", permissionMode="default")
        plan = testing.resolveCompactionExecutionPlan({"config": {}, "session": session})
        self.assertEqual(plan["synthesis"], {"provider": "openai", "providerProfileName": None, "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "fixed", "purpose": "synthesis"})
        self.assertEqual(plan["verification"], {"provider": "openai", "providerProfileName": None, "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "fixed", "purpose": "verification"})

    def test_configured_compaction_profile_overrides_live_session(self):
        profile = {
            "profileName": "codex",
            "profile": {
                "name": "codex",
                "kind": "openai",
                "apiMode": "codex",
                "defaultModel": "gpt-5.3-codex-spark",
                "effortLevel": "high",
                "maxContextLength": 400_000,
                "maxOutputTokens": 128_000,
                "compactionThresholdTokens": 200_000,
                "compactionOutputTokens": 20_000,
            },
            "openaiToken": "codex-token",
            "customHeaders": {},
        }
        session = core.createEmptySession(cwd="/tmp/workspace", provider="anthropic", providerProfileName="anthropic-console", model="claude-opus-4-8", effort="max", systemPrompt="s", permissionMode="default")
        plan = testing.resolveCompactionExecutionPlan({"config": {"compactionProviderProfile": profile}, "session": session})
        self.assertEqual(plan["synthesis"], {"provider": "openai", "providerProfileName": "codex", "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "configured", "purpose": "synthesis"})
        self.assertEqual(plan["verification"], {"provider": "openai", "providerProfileName": "codex", "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "configured", "purpose": "verification"})
        policy = testing.resolveCompactionTokenPolicy({"config": {"compactionProviderProfile": profile, "providerProfiles": {"codex": profile["profile"]}}, "model": plan["synthesis"]})
        self.assertEqual(policy["compactionOutputTokens"], 20_000)
        self.assertEqual(policy["profileName"], "codex")

    def test_configured_profile_carries_auth_while_model_and_effort_are_overridden(self):
        profile = {
            "profileName": "codex",
            "profile": {
                "name": "codex",
                "kind": "openai",
                "apiMode": "codex",
                "defaultModel": "gpt-5.3-codex-spark",
                "effortLevel": "high",
            },
            "openaiToken": "codex-token",
            "customHeaders": {},
        }
        session = core.createEmptySession(
            cwd="/tmp/workspace",
            provider="anthropic",
            providerProfileName="anthropic-console",
            model="claude-opus-4-8",
            effort="max",
            systemPrompt="s",
            permissionMode="default",
        )
        config = {
            "compactionProvider": "openai",
            "compactionProviderProfile": profile,
            "compactionModel": "gpt-5.6-terra",
            "compactionEffort": "medium",
            "compactionVerificationProvider": "openai",
            "compactionVerificationProviderProfile": profile,
            "compactionVerificationModel": "gpt-5.6-terra",
            "compactionVerificationEffort": "medium",
        }
        plan = testing.resolveCompactionExecutionPlan({"config": config, "session": session})
        self.assertEqual(
            plan["synthesis"],
            {
                "provider": "openai",
                "providerProfileName": "codex",
                "model": "gpt-5.6-terra",
                "effort": "medium",
                "selectionSource": "configured",
                "purpose": "synthesis",
            },
        )
        self.assertEqual(
            plan["verification"],
            {
                "provider": "openai",
                "providerProfileName": "codex",
                "model": "gpt-5.6-terra",
                "effort": "medium",
                "selectionSource": "configured",
                "purpose": "verification",
            },
        )
