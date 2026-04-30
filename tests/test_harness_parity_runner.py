from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.model import Scenario, ScenarioStep
from agent_core_harness.parity_runner import run_parity_scenario


class HarnessParityRunnerTests(AgentCoreTestCase):
    def test_runtime_state_parity_pack_matches_two_codebases(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.runtime-state-smoke", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.normalizedPath).read_text(encoding="utf-8"))
            comparison = observations["comparison"]
            python_case = observations["python"]["cases"][0]
            typescript_case = observations["typescript"]["cases"][0]

            self.assertEqual(comparison["status"], "matched")
            self.assertEqual(comparison["classification"], "matched")
            self.assertEqual(python_case["runtimeState"], typescript_case["runtimeState"])
            self.assertEqual(python_case["runtimeState"]["turn"]["status"], "completed")
            self.assertEqual(python_case["storage"]["loadedSessionId"], "parity-session")
            family_names = {family["name"] for family in comparison["families"]}
            self.assertIn("config-session-runtime.runtimeState", family_names)
            self.assertIn("config-session-runtime.runtimeEvents", family_names)

    def test_committed_parity_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.runtime-state-smoke", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_compaction_primitives_pack_matches_two_codebases(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.compaction-primitives-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.normalizedPath).read_text(encoding="utf-8"))
            comparison = observations["comparison"]
            python_case = observations["python"]["cases"][0]
            typescript_case = observations["typescript"]["cases"][0]

            self.assertEqual(comparison["status"], "matched")
            self.assertEqual(python_case["modelPlan"], typescript_case["modelPlan"])
            self.assertEqual(python_case["tokenEstimates"], typescript_case["tokenEstimates"])
            self.assertTrue(python_case["workingMessages"]["compacted"]["hasContinuationBridge"])
            self.assertTrue(python_case["systemPromptContext"]["summaryFallback"]["containsPrimitiveParity"])

    def test_committed_compaction_primitives_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.compaction-primitives-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_parity_mismatch_writes_debug_bundle(self):
        scenario = Scenario(
            "parity.runtime-mismatch",
            "Parity Runtime Mismatch",
            "parity",
            ["parity"],
            {"workspace": {"files": {"README.md": "mismatch\n"}}},
            [
                ScenarioStep(
                    "parity_action",
                    {
                        "action": "runtime_state_smoke",
                        "name": "config-session-runtime",
                        "sessionId": "parity-session",
                        "assistantText": "hello from runtime",
                        "python": {"assistantText": "python drift"},
                    },
                )
            ],
            {"include": ["parity_summary", "parity_python", "parity_typescript", "parity_comparison"]},
        )
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario(scenario, runs_dir=runs)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure["category"], "parity")
            self.assertEqual(result.failure["classification"], "behavior_drift")

            debug_dir = Path(result.debugDir)
            self.assertTrue((debug_dir / "failure.json").exists())
            self.assertTrue((debug_dir / "parity.diff").exists())
            self.assertIn("python drift", (debug_dir / "parity.diff").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
