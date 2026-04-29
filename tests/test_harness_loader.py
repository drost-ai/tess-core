from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.loader import load_scenario, load_scenarios
from agent_core_harness.model import ScenarioValidationError


class HarnessLoaderTests(AgentCoreTestCase):
    def _write(self, directory: Path, name: str, data: dict):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _manifest(self, scenario_id: str = "pack.case") -> dict:
        return {
            "id": scenario_id,
            "title": "Case",
            "area": "pack",
            "mode": ["python"],
            "steps": [{"create_session": {}}],
            "capture": {"include": ["session"]},
        }

    def test_loads_one_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(Path(tmp), "case.json", self._manifest())
            scenarios = load_scenarios(tmp)
            self.assertEqual([scenario.id for scenario in scenarios], ["pack.case"])
            self.assertEqual(load_scenario("pack.case", tmp).steps[0].type, "create_session")

    def test_rejects_missing_id_duplicate_and_unknown_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(Path(tmp), "missing.json", {**self._manifest(), "id": ""})
            with self.assertRaises(ScenarioValidationError):
                load_scenarios(tmp)
        with tempfile.TemporaryDirectory() as tmp:
            self._write(Path(tmp), "a.json", self._manifest("dup.case"))
            self._write(Path(tmp), "b.json", self._manifest("dup.case"))
            with self.assertRaises(ScenarioValidationError):
                load_scenarios(tmp)
        with tempfile.TemporaryDirectory() as tmp:
            self._write(Path(tmp), "bad.json", {**self._manifest(), "steps": [{"unknown_step": {}}]})
            with self.assertRaises(ScenarioValidationError):
                load_scenarios(tmp)

    def test_rejects_unsupported_capture_and_workspace_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(Path(tmp), "bad.json", {**self._manifest(), "capture": {"include": ["not_real"]}})
            with self.assertRaises(ScenarioValidationError):
                load_scenarios(tmp)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest()
            manifest["setup"] = {"workspace": {"files": {"../escape.txt": "bad"}}}
            self._write(Path(tmp), "bad.json", manifest)
            with self.assertRaises(ScenarioValidationError):
                load_scenarios(tmp)

