from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.executor import run_scenario
from agent_core_harness.normalization import normalize_observations
from agent_core_harness.sandbox import ScenarioSandbox


class HarnessGoldenTests(AgentCoreTestCase):
    def test_golden_update_compare_and_mismatch_debug_bundle(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as goldens:
            updated = run_scenario("smoke.basic-session", runs_dir=runs, goldens_dir=goldens, golden="update", cleanup="success")
            self.assertEqual(updated.status, "passed")
            expected_path = Path(updated.golden["expectedPath"])
            self.assertTrue(expected_path.exists())

            matched = run_scenario("smoke.basic-session", runs_dir=runs, goldens_dir=goldens, golden="compare", cleanup="success")
            self.assertEqual(matched.status, "passed")
            self.assertEqual(matched.golden["status"], "matched")

            expected = json.loads(expected_path.read_text(encoding="utf-8"))
            expected["scenario"]["title"] = "Wrong Title"
            expected_path.write_text(json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            mismatched = run_scenario("smoke.basic-session", runs_dir=runs, goldens_dir=goldens, golden="compare")
            self.assertEqual(mismatched.status, "failed")
            self.assertEqual(mismatched.failure["category"], "golden")
            self.assertEqual(mismatched.golden["status"], "mismatched")
            debug_dir = Path(mismatched.debugDir)
            self.assertTrue((debug_dir / "golden.diff").exists())
            self.assertIn("Wrong Title", (debug_dir / "golden.diff").read_text(encoding="utf-8"))

    def test_committed_smoke_and_provider_goldens_compare(self):
        with tempfile.TemporaryDirectory() as runs:
            smoke = run_scenario("smoke.basic-session", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(smoke.status, "passed")
            provider = run_scenario("providers.fake-provider-pack", runs_dir=runs, golden="compare", cleanup="success")
            self.assertEqual(provider.status, "passed")

    def test_normalizer_replaces_dynamic_values_without_erasing_contract_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = ScenarioSandbox("normalize.case", runs_dir=Path(tmp), run_id="20260429-112233-abcdef12").create()
            try:
                raw = {
                    "run": {"id": sandbox.run_id, "pid": 12345, "port": 54321},
                    "url": "http://127.0.0.1:54321/v1/messages",
                    "when": "2026-04-29T18:00:00.000Z",
                    "embedded": '{"createdAt":"2026-04-29T18:00:00.000Z"}',
                    "sessionId": "session-11111111-2222-3333-4444-555555555555",
                    "path": str(sandbox.workspace / "README.md"),
                    "repo": str(Path.cwd() / "agent_core_harness"),
                    "tokens": {"input_tokens": 54321},
                }
                normalized = normalize_observations(raw, sandbox)
                self.assertEqual(normalized["run"]["id"], "<run-id>")
                self.assertEqual(normalized["run"]["pid"], "<pid:1>")
                self.assertEqual(normalized["run"]["port"], "<port:1>")
                self.assertEqual(normalized["url"], "http://127.0.0.1:<port:1>/v1/messages")
                self.assertEqual(normalized["when"], "<timestamp>")
                self.assertEqual(normalized["embedded"], '{"createdAt":"<timestamp>"}')
                self.assertEqual(normalized["sessionId"], "session-<uuid:1>")
                self.assertEqual(normalized["path"], "<workspace>/README.md")
                self.assertEqual(normalized["repo"], "<repo>/agent_core_harness")
                self.assertEqual(normalized["tokens"]["input_tokens"], 54321)
            finally:
                sandbox.cleanup_sandbox(passed=True)
