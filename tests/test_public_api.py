from __future__ import annotations

from tests.support.agent_core_case import *


class PublicApiTests(AgentCoreTestCase):
    def test_public_entrypoints(self):
        self.assertTrue(callable(core.resolveConfig))
        self.assertTrue(callable(core.runAgentPrompt))
        self.assertTrue(callable(core.bootstrapRuntime))
        self.assertTrue(callable(core.create_empty_session))
        self.assertTrue(callable(core.ensureTaskSupervisorDaemon))

    def test_reference_module_surfaces(self):
        import agent_core.platform_support as platform_common
        import agent_core.providers.watchdog as provider_watchdog

        self.assertTrue(callable(provider_watchdog.createProviderRequestWatchdog))
        self.assertEqual(platform_common.buildShellCommand("echo", ["hello world"], {"FOO": "bar baz"}), "FOO='bar baz' echo 'hello world'")
        self.assertEqual(
            subagents_module.sanitizePermissionMode("bogus"),
            "default",
        )
