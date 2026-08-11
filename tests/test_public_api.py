from __future__ import annotations

from tests.support.agent_core_case import *


class PublicApiTests(AgentCoreTestCase):
    def test_public_entrypoints(self):
        self.assertTrue(callable(core.resolve_config))
        self.assertTrue(callable(core.run_agent_prompt))
        self.assertTrue(callable(core.bootstrap_runtime))
        self.assertTrue(callable(core.create_empty_session))
        self.assertTrue(callable(core.ensure_task_supervisor_daemon))

    def test_reference_module_surfaces(self):
        import agent_core.platform_support as platform_common
        import agent_core.providers.watchdog as provider_watchdog

        self.assertTrue(callable(provider_watchdog.create_provider_request_watchdog))
        self.assertEqual(platform_common.build_shell_command("echo", ["hello world"], {"FOO": "bar baz"}), "FOO='bar baz' echo 'hello world'")
        self.assertEqual(
            subagents_module.sanitize_permission_mode("bogus"),
            "default",
        )
