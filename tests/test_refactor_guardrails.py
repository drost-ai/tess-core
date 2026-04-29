from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path

from tests.support.file_size import find_line_budget_violations


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_API_EXPORTS = ROOT / "tests" / "fixtures" / "public_api" / "exports.json"

PUBLIC_MODULES = [
    "agent_core",
    "agent_core.agent",
    "agent_core.compaction",
    "agent_core.config",
    "agent_core.continuation",
    "agent_core.daemon",
    "agent_core.durable_memory",
    "agent_core.interactive_actions",
    "agent_core.mcp",
    "agent_core.platform",
    "agent_core.shell",
    "agent_core.storage",
    "agent_core.subagents",
    "agent_core.task_runtime",
    "agent_core.task_supervisor",
    "agent_core.testing",
    "agent_core.token_accounting",
    "agent_core.tools",
    "agent_core.context.compaction",
    "agent_core.context.continuation",
    "agent_core.providers.anthropic",
    "agent_core.providers.openai",
    "agent_core.tasks.runtime",
    "agent_core.tasks.subagents",
    "agent_core.tasks.supervisor",
]

LINE_BUDGET_ALLOWLIST = {
    "agent_core/agent.py": 732,
    "agent_core/config.py": 878,
    "agent_core/daemon.py": 737,
    "agent_core/platform.py": 854,
    "agent_core/providers/anthropic.py": 537,
    "agent_core/providers/openai.py": 729,
    "agent_core/runtime_events.py": 621,
    "agent_core/runtime_state.py": 711,
    "agent_core/storage.py": 841,
    "product-planning/01-vision-architecture/04-workstreams.md": 600,
}


def _public_names(module_name: str) -> list[str]:
    module = importlib.import_module(module_name)
    return sorted(name for name in dir(module) if not name.startswith("_"))


class RefactorGuardrailTests(unittest.TestCase):
    def test_public_module_exports_match_snapshot(self):
        for module_name in PUBLIC_MODULES:
            importlib.import_module(module_name)

        snapshot = json.loads(PUBLIC_API_EXPORTS.read_text())
        self.assertEqual(sorted(PUBLIC_MODULES), sorted(snapshot))

        for module_name in PUBLIC_MODULES:
            self.assertEqual(snapshot[module_name], _public_names(module_name))

    def test_hand_authored_files_respect_line_budget_or_shrinking_allowlist(self):
        failures = find_line_budget_violations(ROOT, LINE_BUDGET_ALLOWLIST)
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
