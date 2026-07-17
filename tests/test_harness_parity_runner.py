from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.model import Scenario, ScenarioStep
from agent_core_harness.parity_runner import run_parity_scenario


class HarnessParityRunnerTests(AgentCoreTestCase):
    def test_package28_tool_dispatch_parity_matches_golden(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario(
                "parity.package28-tool-dispatch",
                runs_dir=runs,
                golden="compare",
                cleanup="success",
            )
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

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

    def test_compaction_checkpoint_v2_pack_matches_two_codebases(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.compaction-checkpoint-v2-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.normalizedPath).read_text(encoding="utf-8"))
            comparison = observations["comparison"]
            python_case = observations["python"]["cases"][0]
            typescript_case = observations["typescript"]["cases"][0]

            self.assertEqual(comparison["status"], "matched")
            self.assertEqual(python_case["outcome"]["status"], "completed")
            self.assertEqual(python_case["workingContext"], typescript_case["workingContext"])
            self.assertEqual(python_case["checkpoint"], typescript_case["checkpoint"])
            self.assertEqual(python_case["modelCalls"], typescript_case["modelCalls"])

    def test_committed_compaction_checkpoint_v2_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.compaction-checkpoint-v2-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_compaction_repair_fallback_pack_matches_two_codebases(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.compaction-repair-fallback-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.normalizedPath).read_text(encoding="utf-8"))
            comparison = observations["comparison"]
            cases = {case["name"]: case for case in observations["python"]["cases"]}

            self.assertEqual(comparison["status"], "matched")
            self.assertEqual(cases["malformed-synthesis-recovers"]["attempts"]["synthesis"], ["malformed_json", "success"])
            self.assertEqual(cases["incomplete-synthesis-recovers"]["attempts"]["synthesis"], ["incomplete_context", "success"])
            self.assertEqual(cases["verifier-repair-accepted"]["repair"]["repairCount"], 1)
            self.assertEqual(cases["retryable-synthesis-fallback"]["attempts"]["synthesis"], ["retryable_error", "success"])
            self.assertEqual(cases["context-size-synthesis-fallback"]["attempts"]["synthesis"], ["context_size_error", "success"])
            self.assertEqual(cases["verifier-reject-skips"]["skips"]["reason"], "verification_rejected")
            self.assertEqual(cases["low-value-candidate-skips"]["skips"]["reason"], "quality_gate_failed")
            self.assertEqual(cases["unsafe-cutoff-skips"]["skips"]["reason"], "no_legal_cutoff")
            self.assertEqual(cases["unsafe-cutoff-skips"]["skips"]["modelCallCount"], 0)

    def test_committed_compaction_repair_fallback_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.compaction-repair-fallback-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_continuation_relief_pack_matches_two_codebases(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.continuation-relief-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.normalizedPath).read_text(encoding="utf-8"))
            comparison = observations["comparison"]
            cases = {case["name"]: case for case in observations["python"]["cases"]}

            self.assertEqual(comparison["status"], "matched")
            self.assertTrue(cases["lightweight-baseline"]["pressure"]["lightweight"]["applied"])
            self.assertIn("api_microcompact", cases["api-repeated-payload"]["selection"]["appliedModes"])
            self.assertTrue(cases["api-repeated-payload"]["messages"]["hasRepeatedFileOmission"])
            self.assertIn("session_memory_compaction", cases["session-memory-reactive"]["selection"]["appliedModes"])
            self.assertTrue(cases["session-memory-reactive"]["reinjection"]["hasDurableMemory"])
            self.assertIn("partial_compaction", cases["partial-reactive"]["selection"]["appliedModes"])
            self.assertTrue(cases["partial-reactive"]["messages"]["hasPartialSummary"])

    def test_committed_continuation_relief_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_parity_scenario("parity.continuation-relief-pack", runs_dir=runs, golden="compare", cleanup="success")
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
