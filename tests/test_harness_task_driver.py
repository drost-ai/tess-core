from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core_harness.executor import run_scenario
from tess_core_harness.model import Scenario, ScenarioStep


class HarnessTaskDriverTests(TessCoreTestCase):
    def test_daemon_task_pack_runs_and_captures_evidence(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("tasks.daemon-task-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            event_names = [event["event"] for event in observations["daemon_events"]["persisted"]]
            self.assertIn("task.completed", event_names)
            self.assertIn("health_check.recovered", event_names)
            self.assertGreaterEqual(observations["daemon_bootstrap"][0]["monitorSnapshot"]["total"], 1)
            self.assertTrue(any(record["status"] == "stopped" and record["detailStatus"] == "daemon_lost" for record in observations["task_records"]))
            self.assertTrue(any("health-attempt-3" in item["result"]["output"] for item in observations["task_outputs"]))
            self.assertEqual(observations["worker_results"][0]["returnCode"], 0)
            self.assertEqual(observations["worker_results"][0]["output"], "worker-entrypoint-ok")

    def test_committed_daemon_task_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("tasks.daemon-task-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_worker_entrypoint_failure_writes_debug_bundle(self):
        scenario = Scenario(
            "tasks.worker-entrypoint-failure",
            "Worker Entrypoint Failure",
            "tasks",
            ["python"],
            {"workspace": {}},
            [ScenarioStep("run_worker_entrypoint", {"alias": "worker-failure", "command": "exit 7"})],
            {"include": ["task_driver_results", "worker_results", "step_results"]},
        )
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario(scenario, runs_dir=runs)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure["category"], "driver")
            debug_dir = Path(result.debugDir)
            self.assertTrue((debug_dir / "failure.json").exists())
            debug_observations = (debug_dir / "raw-observations.json").read_text(encoding="utf-8")
            self.assertIn('"returnCode": 7', debug_observations)


if __name__ == "__main__":
    unittest.main()
