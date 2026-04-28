from __future__ import annotations

from tests.support.agent_core_case import *


class CompactionModelFetchTests(AgentCoreTestCase):
    def test_fetch_proxy_and_compaction_model_defaults(self):
        self.assertEqual(core.resolveEnvironmentProxyForUrl("https://api.openai.com/v1/responses", {"HTTPS_PROXY": "https-proxy", "HTTP_PROXY": "http-proxy"}), "https-proxy")
        self.assertEqual(core.resolveEnvironmentProxyForUrl("https://api.openai.com/v1/responses", {"HTTP_PROXY": "http-proxy"}), "http-proxy")
        self.assertIsNone(core.resolveEnvironmentProxyForUrl("file:///tmp/a", {"HTTPS_PROXY": "https-proxy"}))

        session = core.createEmptySession(cwd="/tmp/workspace", provider="openai", providerProfileName="openai", model="gpt-5.4", effort="xhigh", systemPrompt="s", permissionMode="default")
        plan = testing.resolveCompactionExecutionPlan({"config": {}, "session": session})
        self.assertEqual(plan["synthesis"], {"provider": "openai", "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "fixed", "purpose": "synthesis"})
        self.assertEqual(plan["verification"], {"provider": "openai", "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "fixed", "purpose": "verification"})
