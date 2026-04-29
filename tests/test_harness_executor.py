from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.executor import run_scenario
from agent_core_harness.loader import load_scenario


class HarnessExecutorTests(AgentCoreTestCase):
    def test_runs_smoke_scenario_successfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario("smoke.basic-session", runs_dir=tmp)
            self.assertEqual(result.status, "passed")
            self.assertTrue(Path(result.observationsPath).exists())
            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            self.assertEqual(observations["scenario"]["id"], "smoke.basic-session")
            self.assertEqual(observations["session"]["provider"], "openai")
            self.assertTrue(any(item["path"] == "README.md" for item in observations["workspace_tree"]))

    def test_failing_assertion_writes_debug_bundle_and_replay(self):
        scenario = load_scenario("smoke.basic-session")
        broken_steps = list(scenario.steps)
        broken_steps[3] = type(broken_steps[3])("assert_session", {"model": "wrong-model"})
        broken = type(scenario)(
            "smoke.failing-session",
            scenario.title,
            scenario.area,
            scenario.modes,
            scenario.setup,
            broken_steps,
            scenario.capture,
            scenario.path,
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario(broken, runs_dir=tmp)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure["category"], "assertion")
            debug_dir = Path(result.debugDir)
            self.assertTrue((debug_dir / "failure.json").exists())
            self.assertIn("agent_core_harness run smoke.failing-session", (debug_dir / "replay.sh").read_text(encoding="utf-8"))

