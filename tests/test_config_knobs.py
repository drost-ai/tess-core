from __future__ import annotations

from agent_core.config._knobs import (
    KNOB_REGISTRY,
    configureKnobs,
    knobValue,
    renderKnobsExample,
    resetConfiguredKnobs,
)
from agent_core.config._yaml import parse_yaml_subset
from tests.support.agent_core_case import *


class KnobRegistryTests(unittest.TestCase):
    def tearDown(self):
        resetConfiguredKnobs()
        for name in list(os.environ):
            if name.startswith("AGENT_CORE_SUBAGENTS_") or name.startswith("AGENT_CORE_PROVIDER_"):
                del os.environ[name]

    def test_defaults_resolve_without_configuration(self):
        self.assertEqual(knobValue("subagents.max_live"), 8)
        self.assertEqual(knobValue("provider.max_output_tokens"), 16384)

    def test_configure_from_nested_mapping_and_reset(self):
        configureKnobs({"subagents": {"max_live": 4}})
        self.assertEqual(knobValue("subagents.max_live"), 4)
        resetConfiguredKnobs()
        self.assertEqual(knobValue("subagents.max_live"), 8)

    def test_unknown_knob_rejected_loudly(self):
        with self.assertRaises(ValueError) as ctx:
            configureKnobs({"subagents": {"max_alive": 4}})
        self.assertIn("subagents.max_alive", str(ctx.exception))
        self.assertIn("subagents.max_live", str(ctx.exception))

    def test_type_and_bounds_validation(self):
        with self.assertRaises(ValueError):
            configureKnobs({"subagents": {"max_live": "many"}})
        with self.assertRaises(ValueError):
            configureKnobs({"subagents": {"max_live": 0}})
        with self.assertRaises(ValueError):
            configureKnobs({"subagents": {"max_live": 65}})
        with self.assertRaises(ValueError):
            configureKnobs({"subagents": {"max_live": True}})

    def test_environment_overrides_file_and_defaults(self):
        os.environ["AGENT_CORE_SUBAGENTS_MAX_LIVE"] = "3"
        self.assertEqual(knobValue("subagents.max_live"), 3)
        configureKnobs({"subagents": {"max_live": 5}})
        self.assertEqual(knobValue("subagents.max_live"), 3)

    def test_invalid_environment_value_rejected(self):
        os.environ["AGENT_CORE_SUBAGENTS_MAX_LIVE"] = "lots"
        with self.assertRaises(ValueError):
            knobValue("subagents.max_live")

    def test_unknown_name_read_is_a_programming_error(self):
        with self.assertRaises(KeyError):
            knobValue("nope.nothing")

    def test_example_file_matches_registry_and_parses_in_subset(self):
        example_path = Path(core.__file__).parent / "config" / "config.yaml.example"
        rendered = renderKnobsExample()
        self.assertEqual(example_path.read_text(encoding="utf-8"), rendered)
        parsed = parse_yaml_subset(rendered, source="config.yaml.example")
        flattened_sections = parsed["knobs"]
        for name, knob in KNOB_REGISTRY.items():
            section, leaf = name.split(".", 1)
            self.assertEqual(flattened_sections[section][leaf], knob.default)


class KnobResolutionIntegrationTests(AgentCoreTestCase):
    def test_resolve_config_installs_knobs_from_settings_folder(self):
        root, home, workspace, previous = self.temp_context()
        try:
            settings_dir = home / "settings"
            settings_dir.mkdir()
            (settings_dir / "config.yaml").write_text(
                "knobs:\n  subagents:\n    max_live: 2\n",
                encoding="utf-8",
            )
            run(core.resolveConfig({}, str(workspace)))
            self.assertEqual(knobValue("subagents.max_live"), 2)
        finally:
            self.restore_home(root, previous)

    def test_resolve_config_fails_loudly_on_unknown_knob(self):
        root, home, workspace, previous = self.temp_context()
        try:
            settings_dir = home / "settings"
            settings_dir.mkdir()
            (settings_dir / "config.yaml").write_text(
                "knobs:\n  daemon:\n    made_up: 1\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                run(core.resolveConfig({}, str(workspace)))
            self.assertIn("daemon.made_up", str(ctx.exception))
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
