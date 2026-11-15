from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path

from tests.support.file_size import find_line_budget_violations


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "tests" / "fixtures" / "public_api" / "contract.json").read_text())

# The internal aggregation facade (tess_core/_facade.py) keeps the full
# historical namespace for the test suite and harness; it shrinks as Epic M
# converts planes. It is not public API and tracks no module complexity.
# test_monitor_delivery.py grew past the budget in 2308beb; its ceiling only
# shrinks, and splitting its shared fixture class is the way back under 500.
LINE_BUDGET_ALLOWLIST = {"tess_core/_facade.py": 800, "tests/test_monitor_delivery.py": 508}


def _active(entrypoint: str) -> list[dict]:
    return [n for n in CONTRACT["names"] if n["entrypoint"] == entrypoint and n["status"] == "active"]


class RefactorGuardrailTests(unittest.TestCase):
    def test_root_entrypoint_exports_exactly_the_contract(self):
        module = importlib.import_module(CONTRACT["entrypoints"]["root"])
        expected = sorted(n["py"] for n in _active("root"))
        self.assertEqual(sorted(module.__all__), expected)
        for name in expected:
            self.assertTrue(hasattr(module, name), f"tess_core is missing contract name {name}")

    def test_entrypoints_export_their_contract_names(self):
        for entrypoint, module_name in CONTRACT["entrypoints"].items():
            module = importlib.import_module(module_name)
            for row in _active(entrypoint):
                self.assertTrue(
                    hasattr(module, row["py"]),
                    f"{module_name} is missing contract name {row['py']} ({row['concept']})",
                )

    def test_contract_homes_still_define_their_names(self):
        for row in CONTRACT["names"]:
            if row["status"] != "active":
                continue
            module = importlib.import_module(row["py_module"])
            self.assertTrue(
                hasattr(module, row["py"]),
                f"{row['py_module']} no longer defines contract name {row['py']}",
            )

    def test_hand_authored_files_respect_line_budget_or_shrinking_allowlist(self):
        failures = find_line_budget_violations(ROOT, LINE_BUDGET_ALLOWLIST)
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
