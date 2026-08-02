from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.executor import run_scenario
from agent_core_harness.model import Scenario, ScenarioStep


class HarnessRemoteDriverTests(AgentCoreTestCase):
    def test_remote_bridge_collaboration_pack_runs_and_captures_evidence(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("remote.remote-bridge-collaboration-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            remote_events = [event["event"] for event in observations["remote_events"]]
            bridge_events = [event["event"] for group in observations["bridge_events"] for event in group["events"]]
            collaboration_events = [event["event"] for event in observations["collaboration_events"]]
            diagnostics = {item["alias"]: item["diagnostics"] for item in observations["bridge_diagnostics"]}
            bootstrap = observations["remote_bootstrap_results"][-1]

            self.assertIn("bridge.started", remote_events)
            self.assertIn("bridge.message", remote_events)
            self.assertIn("bridge.poll_failed", bridge_events)
            self.assertIn("collaboration.snapshot", collaboration_events)
            self.assertIn("subagent.status_changed", collaboration_events)
            self.assertEqual(diagnostics["replay"]["messageCount"], 1)
            self.assertEqual(diagnostics["failure"]["messageCount"], 1)
            self.assertTrue(bootstrap["hasBridge"])
            self.assertTrue(bootstrap["hasCollaboration"])
            self.assertIn("Agent", bootstrap["toolNames"])
            self.assertEqual(
                [event["event"] for event in observations["remote_bootstrap_events"]],
                ["session.activated"],
            )
            self.assertEqual(observations["remote_runtime_state"]["bridge"]["lastEventName"], "remote.status_changed")

    def test_committed_remote_bridge_collaboration_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("remote.remote-bridge-collaboration-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_unexpected_remote_assertion_failure_writes_debug_bundle(self):
        scenario = Scenario(
            "remote.bridge-diagnostics-failure",
            "Remote Bridge Diagnostics Failure",
            "remote",
            ["python"],
            {"workspace": {}},
            [
                ScenarioStep("create_session", {"permissionMode": "bypassPermissions"}),
                ScenarioStep("create_remote_bridge", {"alias": "main", "remoteId": "remote-debug", "bridgeId": "bridge-debug", "description": "Debug Remote"}),
                ScenarioStep("queue_bridge_message", {"alias": "main", "text": "debug payload", "source": "remote"}),
                ScenarioStep("collect_bridge_diagnostics", {"alias": "main"}),
                ScenarioStep("assert_bridge_diagnostics", {"alias": "main", "status": "failed"}),
            ],
            {"include": ["remote_driver_results", "remote_sessions", "bridge_records", "bridge_events", "bridge_diagnostics", "step_results"]},
        )
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario(scenario, runs_dir=runs)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure["category"], "assertion")

            debug_dir = Path(result.debugDir)
            self.assertTrue((debug_dir / "failure.json").exists())
            debug_observations = (debug_dir / "raw-observations.json").read_text(encoding="utf-8")
            self.assertIn("debug payload", debug_observations)
            self.assertIn("bridge.message.queued", debug_observations)


if __name__ == "__main__":
    unittest.main()
