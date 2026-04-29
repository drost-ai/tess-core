from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.executor import run_scenario
from agent_core_harness.model import Scenario, ScenarioStep


class HarnessContextDriverTests(AgentCoreTestCase):
    def test_context_management_pack_runs_and_captures_evidence(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("context.context-management-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            compactions = observations["compaction_results"]
            continuations = observations["continuation_results"]
            agent_results = observations["context_agent_results"]
            event_names = [event["event"] for event in observations["context_events"]]

            self.assertEqual(compactions[0]["result"]["qualityClassification"], "rich")
            self.assertEqual(compactions[2]["result"]["qualityClassification"], "usable")
            self.assertEqual(compactions[1]["clientCallCount"], 2)
            self.assertIn("Use fixed Codex Spark", json.dumps(observations["durable_memory"]))
            self.assertTrue(any("session_memory_compaction" in item["summary"]["appliedModes"] for item in continuations))
            self.assertTrue(any("partial_compaction" in item["summary"]["appliedModes"] for item in continuations))
            self.assertIn("context_relief.applied", event_names)
            self.assertIn("turn.cancelled", event_names)
            self.assertEqual(agent_results[0]["requestCount"], 2)
            self.assertEqual(agent_results[1]["result"]["stopReason"], "cancelled")

    def test_committed_context_management_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("context.context-management-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_rejected_compaction_writes_debug_bundle(self):
        scenario = Scenario(
            "context.compaction-rejected",
            "Context Compaction Rejected",
            "context",
            ["python"],
            {"workspace": {}},
            [
                ScenarioStep("seed_context_session", {"alias": "reject", "messageCount": 34}),
                ScenarioStep("run_context_compaction", {"mode": "reject", "resultAlias": "reject"}),
            ],
            {"include": ["compaction_results", "compaction_artifacts", "context_driver_results", "step_results"]},
        )
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario(scenario, runs_dir=runs)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure["category"], "driver")

            debug_dir = Path(result.debugDir)
            self.assertTrue((debug_dir / "failure.json").exists())
            debug_observations = (debug_dir / "raw-observations.json").read_text(encoding="utf-8")
            self.assertIn("verification_rejected", debug_observations)


if __name__ == "__main__":
    unittest.main()
