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
                    "unittest": "Ran 12 tests in 1.234s",
                    "sessionId": "session-11111111-2222-3333-4444-555555555555",
                    "path": str(sandbox.workspace / "README.md"),
                    "repo": str(Path.cwd() / "agent_core_harness"),
                    "python": f"{Path.cwd()}/.venv/bin/python",
                    "python3": f"{Path.cwd()}/.venv/bin/python3",
                    "cursor": "acp1.eyJ2IjoxLCJzIjoiZHluYW1pYyJ9",
                    "embeddedCursor": '{"nextCursor":"acp1.eyJ2IjoxLCJzIjoiZHluYW1pYyJ9"}',
                    "tokens": {"input_tokens": 54321},
                    "headroom": {
                        "compactionTargetTokens": 150_000,
                        "compactionThresholdTokens": 200_000,
                        "inputTokens": 3_574,
                        "irreducibleInputTokens": 3_278,
                        "remainingHeadroomTokens": 196_426,
                    },
                    "measurement": {
                        "measurement_source": "openai_responses_estimate",
                        "input_tokens": 3_574,
                        "message_input_tokens": 296,
                        "remaining_headroom_tokens": 196_426,
                    },
                    "interactiveRelief": {
                        "applied": True,
                        "originalInputTokens": 110_399,
                        "projectedInputTokens": 47_500,
                        "thresholdTokens": 64_000,
                        "targetTokens": 48_000,
                    },
                    "processIdentity": {
                        "expectedCommandDigest": "a" * 64,
                        "observedCommandDigest": "b" * 64,
                        "startToken": "ps:Wed Apr 29 11:22:33 2026",
                    },
                    "health": {
                        "specDigest": "c" * 64,
                        "nested": {"spec_digest": "c" * 64},
                        "next": {"specDigest": "d" * 64},
                        "authorityFingerprint": "e" * 64,
                        "unrelatedSha256": "f" * 64,
                    },
                    "turnProgress": {
                        "batchOccurrences": {
                            "1" * 24: 2,
                            "2" * 24: 1,
                        },
                        "lastBatchFingerprint": "1" * 24,
                        "recentBatchFingerprints": ["2" * 24, "1" * 24],
                    },
                }
                normalized = normalize_observations(raw, sandbox)
                self.assertEqual(normalized["run"]["id"], "<run-id>")
                self.assertEqual(normalized["run"]["pid"], "<pid:1>")
                self.assertEqual(normalized["run"]["port"], "<port:1>")
                self.assertEqual(normalized["url"], "http://127.0.0.1:<port:1>/v1/messages")
                self.assertEqual(normalized["when"], "<timestamp>")
                self.assertEqual(normalized["embedded"], '{"createdAt":"<timestamp>"}')
                self.assertEqual(normalized["unittest"], "Ran 12 tests in <duration-s>")
                self.assertEqual(normalized["sessionId"], "session-<uuid:1>")
                self.assertEqual(normalized["path"], "<workspace>/README.md")
                self.assertEqual(normalized["repo"], "<repo>/agent_core_harness")
                self.assertEqual(normalized["python"], "<python>")
                self.assertEqual(normalized["python3"], "<python>")
                self.assertEqual(normalized["cursor"], "<payload-cursor:1>")
                self.assertEqual(
                    normalized["embeddedCursor"],
                    '{"nextCursor":"<payload-cursor:1>"}',
                )
                self.assertEqual(normalized["tokens"]["input_tokens"], 54321)
                self.assertEqual(
                    normalized["headroom"],
                    {
                        "compactionTargetTokens": 150_000,
                        "compactionThresholdTokens": 200_000,
                        "inputTokens": "<token-estimate>",
                        "irreducibleInputTokens": "<token-estimate>",
                        "remainingHeadroomTokens": "<token-estimate>",
                    },
                )
                self.assertEqual(
                    normalized["measurement"]["message_input_tokens"],
                    "<token-estimate>",
                )
                self.assertEqual(
                    normalized["measurement"]["remaining_headroom_tokens"],
                    "<token-estimate>",
                )
                self.assertEqual(
                    normalized["interactiveRelief"],
                    {
                        "applied": True,
                        "originalInputTokens": "<token-estimate>",
                        "projectedInputTokens": "<token-estimate>",
                        "thresholdTokens": 64_000,
                        "targetTokens": 48_000,
                    },
                )
                self.assertEqual(normalized["processIdentity"]["expectedCommandDigest"], "<expected-command-digest>")
                self.assertEqual(normalized["processIdentity"]["observedCommandDigest"], "<observed-command-digest>")
                self.assertEqual(normalized["processIdentity"]["startToken"], "<process-start-token>")
                self.assertEqual(normalized["health"]["specDigest"], "<health-spec-digest:1>")
                self.assertEqual(normalized["health"]["nested"]["spec_digest"], "<health-spec-digest:1>")
                self.assertEqual(normalized["health"]["next"]["specDigest"], "<health-spec-digest:2>")
                self.assertEqual(
                    normalized["health"]["authorityFingerprint"],
                    "<health-authority-fingerprint:1>",
                )
                self.assertEqual(normalized["health"]["unrelatedSha256"], "f" * 64)
                self.assertEqual(
                    normalized["turnProgress"],
                    {
                        "batchOccurrences": {
                            "<progress-fingerprint:1>": 2,
                            "<progress-fingerprint:2>": 1,
                        },
                        "lastBatchFingerprint": "<progress-fingerprint:1>",
                        "recentBatchFingerprints": [
                            "<progress-fingerprint:2>",
                            "<progress-fingerprint:1>",
                        ],
                    },
                )
            finally:
                sandbox.cleanup_sandbox(passed=True)

    def test_normalizer_replaces_home_compacted_sandbox_paths(self):
        sandbox = ScenarioSandbox(
            "normalize.case",
            runs_dir=Path.home() / "agent-core-harness-runs",
            run_id="20260429-112233-abcdef12",
        )
        compact_workspace = f"~/{sandbox.workspace.resolve().relative_to(Path.home().resolve()).as_posix()}"
        normalized = normalize_observations(
            {"summary": f"{compact_workspace} · 0 bytes output"},
            sandbox,
        )
        self.assertEqual(normalized["summary"], "<workspace> · 0 bytes output")
