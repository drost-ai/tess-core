from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.executor import run_scenario


class HarnessStorageDriverTests(AgentCoreTestCase):
    def test_storage_concurrency_pack_runs_and_captures_evidence(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("storage.concurrency-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            subprocess_stdout = [item["stdout"] for item in observations["subprocess_results"]]
            self.assertTrue(any("session-task-ids=4" in stdout for stdout in subprocess_stdout))
            self.assertTrue(any("health-stop-sticky=stopped" in stdout for stdout in subprocess_stdout))

    def test_committed_storage_concurrency_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("storage.concurrency-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")
