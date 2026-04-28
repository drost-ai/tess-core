from __future__ import annotations

import unittest


DOMAIN_TEST_MODULES = [
    "tests.test_public_api",
    "tests.test_tools_contracts",
    "tests.test_tools_payloads_search",
    "tests.test_tools_control_flow",
    "tests.test_config_profiles",
    "tests.test_providers_openai_chat",
    "tests.test_providers_openai_responses",
    "tests.test_providers_anthropic",
    "tests.test_provider_errors",
    "tests.test_agent_loop",
    "tests.test_compaction_models_fetch",
    "tests.test_runtime_state_core",
    "tests.test_runtime_state_projection",
    "tests.test_runtime_bootstrap_plugins",
    "tests.test_runtime_bootstrap_lifecycle",
    "tests.test_collaboration_runtime",
    "tests.test_remote_bridge",
    "tests.test_shell_commands",
    "tests.test_shell_remote_background",
    "tests.test_transcript_shell_compaction",
    "tests.test_platform_surface",
    "tests.test_context_compaction",
    "tests.test_context_continuation",
    "tests.test_task_supervisor_core",
    "tests.test_task_supervisor_lifecycle",
    "tests.test_task_supervisor_reconciliation",
    "tests.test_task_monitor_health",
    "tests.test_tools_permissions",
    "tests.test_tools_agent",
    "tests.test_tools_worktree_notebook",
    "tests.test_mcp_stdio_sse",
    "tests.test_mcp_runtime_edges",
    "tests.test_mcp_cancellation_blob_plugin",
]


def load_tests(loader, tests, pattern):
    if pattern is not None:
        return unittest.TestSuite()
    suite = unittest.TestSuite()
    for module in DOMAIN_TEST_MODULES:
        suite.addTests(loader.loadTestsFromName(module))
    return suite


if __name__ == "__main__":
    unittest.main()
