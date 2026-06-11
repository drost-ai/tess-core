from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.executor import run_scenario
from agent_core_harness.model import Scenario, ScenarioStep


class HarnessRuntimeDriverTests(AgentCoreTestCase):
    def test_runtime_tool_pack_runs_and_captures_evidence(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("runtime.runtime-tool-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            event_names = [event["event"] for event in observations["runtime_events"]]
            driver_names = [record["driver"] for record in observations["driver_results"]]

            for event in ("session.activated", "message.user", "tool.started", "tool.completed", "message.assistant"):
                self.assertIn(event, event_names)
            for driver in ("bootstrap_runtime", "run_agent", "run_subprocess", "run_slash_command", "call_tool"):
                self.assertIn(driver, driver_names)

            self.assertEqual(observations["runtime_state"]["turn"]["status"], "idle")
            self.assertIn("subprocess ok", observations["subprocess_results"][0]["stdout"])
            self.assertIn("README says hello from runtime harness.", json.dumps(observations["transcript"]))
            artifact_paths = {item["path"] for item in observations["tool_artifacts"]}
            self.assertTrue(any(path.endswith(".meta.json") for path in artifact_paths))
            self.assertTrue(any(path.endswith(".txt") for path in artifact_paths))

    def test_committed_runtime_tool_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("runtime.runtime-tool-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_headless_json_stream_pack_runs(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("runtime.headless-json-stream-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")
            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            stdout = observations["subprocess_results"][0]["stdout"]
            self.assertIn('"event":"system.init"', stdout)
            self.assertIn('"event":"result"', stdout)
            self.assertIn('"text":"hello from stream"', stdout)

    def test_committed_headless_json_stream_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("runtime.headless-json-stream-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_unexpected_driver_failure_writes_debug_bundle(self):
        scenario = Scenario(
            "runtime.subprocess-failure",
            "Runtime Subprocess Failure",
            "runtime",
            ["python"],
            {"workspace": {}},
            [
                ScenarioStep(
                    "run_subprocess",
                    {"command": ["python3", "-c", "import sys\nprint('bad exit')\nsys.exit(7)"]},
                )
            ],
            {"include": ["driver_results", "subprocess_results", "step_results"]},
        )
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario(scenario, runs_dir=runs)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure["category"], "driver")

            debug_dir = Path(result.debugDir)
            self.assertTrue((debug_dir / "failure.json").exists())
            debug_observations = (debug_dir / "raw-observations.json").read_text(encoding="utf-8")
            self.assertIn("bad exit", debug_observations)
            self.assertIn('"returnCode": 7', debug_observations)


if __name__ == "__main__":
    unittest.main()
