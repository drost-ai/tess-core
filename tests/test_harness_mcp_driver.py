from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.executor import run_scenario
from agent_core_harness.model import Scenario, ScenarioStep


class HarnessMcpDriverTests(AgentCoreTestCase):
    def test_package28_hardening_pack_runs_and_matches_golden(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario(
                "mcp.package28-hardening-pack",
                runs_dir=runs,
                golden="compare",
                cleanup="never",
            )
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

            observations = json.loads(
                Path(result.observationsPath).read_text(encoding="utf-8")
            )
            probes = {
                item["probe"]: item for item in observations["mcp_package28_results"]
            }
            self.assertEqual(
                set(probes),
                {"dispatch", "protocol", "catalog_boundary", "headless_auth"},
            )
            self.assertTrue(probes["dispatch"]["parallelOverlapped"])
            self.assertEqual(probes["headless_auth"]["afterState"], "connected")

    def test_fake_mcp_pack_runs_and_captures_evidence(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("mcp.fake-mcp-pack", runs_dir=runs)
            self.assertEqual(result.status, "passed")

            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            event_names = [event["event"] for event in observations["mcp_events"]]
            result_body = json.dumps(observations["mcp_results"])
            plugin_result = observations["mcp_plugin_results"][-1]

            for event in ("mcp.server_connected", "mcp.channel_registered", "mcp.tools_changed", "mcp.resources_changed", "mcp.log"):
                self.assertIn(event, event_names)
            self.assertIn("HTTP 500 from MCP server http", result_body)
            self.assertIn("batch JSON-RPC response", result_body)
            self.assertIn("blobSavedTo", result_body)
            self.assertIn("mcp__shared__plugin_tool", plugin_result["toolNames"])
            self.assertEqual(plugin_result["serverStatuses"][0]["serverInfo"]["name"], "plugin-shared")

    def test_committed_fake_mcp_golden_compares(self):
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario("mcp.fake-mcp-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.golden["status"], "matched")

    def test_unexpected_protocol_failure_writes_debug_bundle(self):
        scenario = Scenario(
            "mcp.protocol-failure",
            "MCP Protocol Failure",
            "mcp",
            ["python"],
            {"workspace": {}},
            [
                ScenarioStep("start_fake_mcp", {"alias": "protocol", "name": "protocol", "transport": "http", "profile": "protocol", "toolName": "edge"}),
                ScenarioStep("create_mcp_runtime", {}),
                ScenarioStep("call_mcp_tool", {"server": "protocol", "tool": "edge", "input": {"variant": "batch"}}),
            ],
            {"include": ["mcp_results", "mcp_status", "mcp_events", "step_results"]},
        )
        with tempfile.TemporaryDirectory() as runs:
            result = run_scenario(scenario, runs_dir=runs)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure["category"], "driver")

            debug_dir = Path(result.debugDir)
            self.assertTrue((debug_dir / "failure.json").exists())
            debug_observations = (debug_dir / "raw-observations.json").read_text(encoding="utf-8")
            self.assertIn("batch JSON-RPC response", debug_observations)


if __name__ == "__main__":
    unittest.main()
