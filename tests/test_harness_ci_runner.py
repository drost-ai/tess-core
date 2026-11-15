from __future__ import annotations

import xml.etree.ElementTree as ET

from tests.support.agent_core_case import *

from agent_core_harness.ci_runner import run_ci_suite, select_scenarios
from agent_core_harness.loader import load_scenarios


class HarnessCiRunnerTests(AgentCoreTestCase):
    def test_ci_suite_runs_selected_smoke_and_writes_reports(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as reports:
            result = run_ci_suite(
                runs_dir=runs,
                reports_dir=reports,
                scenario_ids=["smoke.basic-session"],
                golden="off",
                cleanup="success",
            )
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.counts["passed"], 1)

            summary = json.loads(Path(result.reports["json"]).read_text(encoding="utf-8"))
            markdown = Path(result.reports["markdown"]).read_text(encoding="utf-8")
            junit = ET.fromstring(Path(result.reports["junit"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["records"][0]["scenarioId"], "smoke.basic-session")
            self.assertIn("python3 -m agent_core_harness", markdown)
            self.assertIn("--golden update", markdown)
            self.assertEqual(junit.attrib["tests"], "1")
            self.assertEqual(junit.attrib["failures"], "0")

    def test_changed_area_selection_includes_smoke_baseline(self):
        selected = select_scenarios(load_scenarios(), [], [], ["runtime"])
        ids = {scenario.id for scenario in selected}

        self.assertIn("smoke.basic-session", ids)
        self.assertIn("runtime.runtime-tool-pack", ids)
        self.assertNotIn("providers.fake-provider-pack", ids)

    def test_ci_suite_skips_disabled_capability(self):
        with tempfile.TemporaryDirectory() as scenarios, tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as reports:
            (Path(scenarios) / "needs-node.json").write_text(json.dumps({
                "id": "ci.needs-node", "title": "Needs Node", "area": "smoke", "mode": ["python"],
                "capabilities": ["node"],
                "steps": [{"create_session": {"permissionMode": "bypassPermissions"}}],
                "capture": {"include": ["session"]},
            }), encoding="utf-8")
            result = run_ci_suite(
                scenarios_dir=scenarios,
                runs_dir=runs,
                reports_dir=reports,
                scenario_ids=["ci.needs-node"],
                golden="off",
                disabled_capabilities=["node"],
            )
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.counts["skipped"], 1)

            record = result.records[0]
            self.assertEqual(record.status, "skipped")
            self.assertIn("node", record.reason)
            junit = ET.fromstring(Path(result.reports["junit"]).read_text(encoding="utf-8"))
            self.assertEqual(junit.attrib["skipped"], "1")

    def test_ci_suite_failure_report_includes_debug_and_junit_failure(self):
        with tempfile.TemporaryDirectory() as scenarios, tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as reports:
            path = Path(scenarios) / "failing.json"
            path.write_text(
                json.dumps(
                    {
                        "id": "ci.failing-session",
                        "title": "CI Failing Session",
                        "area": "smoke",
                        "mode": ["python"],
                        "steps": [
                            {"create_session": {"permissionMode": "bypassPermissions"}},
                            {"assert_session": {"model": "wrong-model"}},
                        ],
                        "capture": {"include": ["session", "step_results"]},
                    }
                ),
                encoding="utf-8",
            )
            result = run_ci_suite(
                scenarios_dir=scenarios,
                runs_dir=runs,
                reports_dir=reports,
                scenario_ids=["ci.failing-session"],
                golden="off",
                cleanup="never",
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.counts["failed"], 1)

            markdown = Path(result.reports["markdown"]).read_text(encoding="utf-8")
            junit = ET.fromstring(Path(result.reports["junit"]).read_text(encoding="utf-8"))
            failure = junit.find("testcase/failure")

            self.assertIn("ci.failing-session", markdown)
            self.assertIn("Debug:", markdown)
            self.assertIsNotNone(failure)
            self.assertIn("wrong-model", failure.text)


if __name__ == "__main__":
    unittest.main()
