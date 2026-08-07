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
        self.assertEqual(knobValue("provider.request_start_timeout_ms"), 120_000)
        self.assertEqual(knobValue("provider.stream_idle_timeout_ms"), 60_000)
        self.assertEqual(knobValue("provider.json_response_timeout_ms"), 120_000)
        self.assertEqual(knobValue("provider.openai_retry_delays_ms"), (1_000, 3_000, 5_000, 10_000, 30_000))
        self.assertEqual(knobValue("provider.anthropic_retry_delays_ms"), (350, 700))

    def test_int_list_coercion_and_bounds(self):
        configureKnobs({"provider": {"openai_retry_delays_ms": [1, 2, 3]}})
        self.assertEqual(knobValue("provider.openai_retry_delays_ms"), (1, 2, 3))
        for bad in ([], ["fast"], [True], [-1], [700_000], 1000):
            with self.assertRaises(ValueError):
                configureKnobs({"provider": {"openai_retry_delays_ms": bad}})

    def test_int_list_environment_override_comma_separated(self):
        os.environ["AGENT_CORE_PROVIDER_OPENAI_RETRY_DELAYS_MS"] = "10, 20,30"
        self.assertEqual(knobValue("provider.openai_retry_delays_ms"), (10, 20, 30))

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
            expected = list(knob.default) if isinstance(knob.default, tuple) else knob.default
            self.assertEqual(flattened_sections[section][leaf], expected)


class KnobWiringTests(unittest.TestCase):
    """The provider-facing knobs actually drive the provider machinery."""

    def tearDown(self):
        resetConfiguredKnobs()
        for name in list(os.environ):
            if name.startswith("AGENT_CORE_PROVIDER_"):
                del os.environ[name]

    def test_openai_retry_ladder_reads_knob(self):
        delay = openai_module.shared.providerRetryDelayMs
        self.assertEqual(delay(1), 1_000)
        self.assertEqual(delay(5), 30_000)
        self.assertEqual(delay(9), 30_000)
        configureKnobs({"provider": {"openai_retry_delays_ms": [5, 7]}})
        self.assertEqual(delay(1), 5)
        self.assertEqual(delay(3), 7)

    def test_anthropic_quick_retry_reads_knob_with_jitter(self):
        from agent_core.providers.errors import providerRetryDelayMs

        first, second = providerRetryDelayMs(1), providerRetryDelayMs(2)
        self.assertTrue(350 <= first <= 469, first)
        self.assertTrue(700 <= second <= 819, second)
        configureKnobs({"provider": {"anthropic_retry_delays_ms": [0]}})
        self.assertTrue(0 <= providerRetryDelayMs(3) <= 119)

    def test_turn_deadline_context_timeout_fallbacks_read_knobs(self):
        from agent_core.providers.timeouts import createTurnDeadlineContext

        context = createTurnDeadlineContext(config={})
        try:
            self.assertEqual(context.requestStartTimeoutMs, 120_000)
            self.assertEqual(context.streamIdleTimeoutMs, 60_000)
        finally:
            context.dispose()
        configureKnobs({"provider": {"request_start_timeout_ms": 11_000, "stream_idle_timeout_ms": 12_000}})
        context = createTurnDeadlineContext(config={})
        try:
            self.assertEqual(context.requestStartTimeoutMs, 11_000)
            self.assertEqual(context.streamIdleTimeoutMs, 12_000)
        finally:
            context.dispose()
        context = createTurnDeadlineContext(
            config={"providerProfile": {"requestStartTimeoutMs": 9_000}}
        )
        try:
            self.assertEqual(context.requestStartTimeoutMs, 9_000)
        finally:
            context.dispose()

    def test_request_max_output_tokens_fallback_reads_knob(self):
        from agent_core.providers.token_budget import resolve_request_max_output_tokens

        self.assertEqual(resolve_request_max_output_tokens({"maxOutputTokens": 128_000}), 128_000)
        self.assertEqual(resolve_request_max_output_tokens({"max_tokens": "2048"}), 2_048)
        self.assertEqual(resolve_request_max_output_tokens({}), 16_384)
        configureKnobs({"provider": {"max_output_tokens": 9_999}})
        self.assertEqual(resolve_request_max_output_tokens({}), 9_999)
        self.assertEqual(resolve_request_max_output_tokens({"maxOutputTokens": 1_000}), 1_000)


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
