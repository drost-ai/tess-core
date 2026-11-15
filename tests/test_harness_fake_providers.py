from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core_harness.executor import run_scenario
from tess_core_harness.loader import load_scenario


class HarnessFakeProviderTests(TessCoreTestCase):
    def test_provider_pack_runs_and_captures_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario("providers.fake-provider-pack", runs_dir=tmp)
            self.assertEqual(result.status, "passed")
            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            self.assertEqual(observations["provider_state"]["resultStatuses"], ["passed", "passed", "passed", "failed", "failed"])
            self.assertEqual(len(observations["provider_requests"]), 7)
            self.assertTrue(any(request["api"] == "count_tokens" and request["body"]["model"] == "claude-sonnet-4-6" for request in observations["provider_requests"]))
            event_names = [event["event"] for event in observations["provider_events"]]
            self.assertIn("provider.retrying", event_names)
            self.assertIn("provider.recovered", event_names)
            self.assertIn("provider.stream_event", event_names)
            self.assertEqual(observations["provider_response"][3]["failure"]["category"], "timeout")
            self.assertEqual(observations["provider_response"][4]["failure"]["category"], "cancelled")

    def test_unexpected_provider_failure_writes_debug_bundle(self):
        scenario = load_scenario("providers.fake-provider-pack")
        steps = list(scenario.steps)
        broken = type(steps[0])(
            "provider_request",
            {
                "provider": "openai",
                "api": "responses",
                "script": {"attempts": [{"createError": "hard fail"}]},
            },
        )
        mutated = type(scenario)(
            "providers.unexpected-provider-failure",
            scenario.title,
            scenario.area,
            scenario.modes,
            scenario.setup,
            [broken],
            scenario.capture,
            scenario.path,
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario(mutated, runs_dir=tmp)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure["category"], "provider")
            self.assertTrue((Path(result.debugDir) / "failure.json").exists())

    def test_provider_scenario_is_listed(self):
        scenario = load_scenario("providers.fake-provider-pack")
        self.assertEqual(scenario.area, "providers")
        self.assertEqual(scenario.steps[0].type, "provider_request")
