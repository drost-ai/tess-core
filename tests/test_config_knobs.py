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

    def test_image_capability_knobs_reach_resolved_builtin_profiles(self):
        from agent_core.config.providers import resolveProviderProfile
        from agent_core.config.shared import BUILTIN_PROVIDER_PROFILES

        configureKnobs({"images": {"max_count": 3}})
        try:
            resolved = resolveProviderProfile("openai", BUILTIN_PROVIDER_PROFILES["openai"])
            # Baked import-time values used to shadow this override.
            self.assertEqual(resolved["capabilities"]["maxImageCount"], 3)
            self.assertNotIn("maxImageCount", BUILTIN_PROVIDER_PROFILES["openai"]["capabilities"])
        finally:
            resetConfiguredKnobs()

    def test_openai_retry_ladder_reads_knob_with_jitter(self):
        delay = openai_module.shared.providerRetryDelayMs
        self.assertTrue(1_000 <= delay(1) <= 1_119, delay(1))
        self.assertTrue(30_000 <= delay(5) <= 30_119, delay(5))
        self.assertTrue(30_000 <= delay(9) <= 30_119, delay(9))
        configureKnobs({"provider": {"openai_retry_delays_ms": [5, 7]}})
        self.assertTrue(5 <= delay(1) <= 124, delay(1))
        self.assertTrue(7 <= delay(3) <= 126, delay(3))

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


class DaemonKnobWiringTests(unittest.TestCase):
    """The E2 daemon/runtime/storage timing knobs drive their machinery."""

    def tearDown(self):
        resetConfiguredKnobs()
        for name in list(os.environ):
            if name.startswith(("AGENT_CORE_DAEMON_", "AGENT_CORE_STORAGE_", "AGENT_CORE_RUNTIME_")):
                del os.environ[name]

    def test_daemon_rpc_and_auth_timeouts_read_knobs_and_legacy_env(self):
        from agent_core.daemon.protocol import resolveDaemonAuthTimeoutMs, resolveDaemonRpcTimeoutMs

        self.assertEqual(resolveDaemonRpcTimeoutMs(), 30_000)
        self.assertEqual(resolveDaemonAuthTimeoutMs(), 2_000)
        configureKnobs({"daemon": {"rpc_timeout_ms": 12_000, "auth_timeout_ms": 900}})
        self.assertEqual(resolveDaemonRpcTimeoutMs(), 12_000)
        self.assertEqual(resolveDaemonAuthTimeoutMs(), 900)
        self.assertEqual(resolveDaemonRpcTimeoutMs(7_000), 7_000)  # per-call override still wins
        resetConfiguredKnobs()
        # The legacy env names ARE the canonical knob env overrides and
        # apply even without configureKnobs (bare library use).
        os.environ["AGENT_CORE_DAEMON_RPC_TIMEOUT_MS"] = "5000"
        self.assertEqual(resolveDaemonRpcTimeoutMs(), 5_000)

    def test_daemon_startup_and_instance_lock_timeouts_read_knobs(self):
        from agent_core.daemon.lifecycle import _daemon_instance_wait_ms, _resolve_startup_timeout_seconds

        self.assertEqual(_resolve_startup_timeout_seconds({}), 30.0)
        self.assertEqual(_resolve_startup_timeout_seconds({"startTimeoutMs": 1_500}), 1.5)
        self.assertEqual(_daemon_instance_wait_ms(), 5_000)
        configureKnobs({"daemon": {"start_timeout_ms": 8_000, "instance_lock_timeout_ms": 750}})
        self.assertEqual(_resolve_startup_timeout_seconds({}), 8.0)
        self.assertEqual(_daemon_instance_wait_ms(), 750)
        os.environ["AGENT_CORE_DAEMON_START_TIMEOUT_MS"] = "2000"
        configureKnobs({})
        # Legacy env keeps beating per-call args, as it always has.
        self.assertEqual(_resolve_startup_timeout_seconds({"startTimeoutMs": 9_000}), 2.0)

    def test_boot_grace_dead_work_and_cache_ttl_read_knobs(self):
        from agent_core.daemon.lifecycle import _daemon_state_is_still_booting
        from agent_core.tasks.supervisor_parts.live_work import _create_live_work_verdict_cache

        import datetime

        ten_minutes_ago = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
        ).isoformat().replace("+00:00", "Z")
        booting = {"phase": "starting", "startedAt": ten_minutes_ago}
        self.assertFalse(_daemon_state_is_still_booting(booting))  # past the default 5min grace
        configureKnobs({"daemon": {"boot_grace_ms": 3_600_000, "live_work_cache_ttl_ms": 100}})
        self.assertTrue(_daemon_state_is_still_booting(booting))  # within the widened grace
        cache = _create_live_work_verdict_cache()
        self.assertEqual(cache["getStats"]()["ttlMs"], 100)
        self.assertEqual(_create_live_work_verdict_cache(5_000)["getStats"]()["ttlMs"], 100)  # knob caps explicit ttl

    def test_storage_lock_timeout_reads_knob(self):
        from agent_core.storage.json_io import _storage_lock_timeout_ms

        self.assertEqual(_storage_lock_timeout_ms(), 5_000)
        configureKnobs({"storage": {"lock_timeout_ms": 1_500}})
        self.assertEqual(_storage_lock_timeout_ms(), 1_500)


class ContextKnobWiringTests(unittest.TestCase):
    """The E3 compaction/memory/context knobs drive their machinery."""

    def tearDown(self):
        resetConfiguredKnobs()
        for name in ("TESS_COMPACTION_SYNTHESIS_TIMEOUT_MS", "TESS_COMPACTION_VERIFICATION_TIMEOUT_MS"):
            os.environ.pop(name, None)

    def test_compaction_model_timeouts_read_knob_with_legacy_tess_alias(self):
        from agent_core.context.compaction_transport import _resolve_compaction_timeout_ms

        self.assertEqual(_resolve_compaction_timeout_ms("synthesis"), 300_000)
        configureKnobs({"compaction": {"synthesis_timeout_ms": 45_000, "verification_timeout_ms": 55_000}})
        self.assertEqual(_resolve_compaction_timeout_ms("synthesis"), 45_000)
        self.assertEqual(_resolve_compaction_timeout_ms("verification"), 55_000)
        os.environ["TESS_COMPACTION_SYNTHESIS_TIMEOUT_MS"] = "77000"
        self.assertEqual(_resolve_compaction_timeout_ms("synthesis"), 77_000)  # legacy env stays an alias

    def test_compaction_relief_and_memory_budgets_read_knobs(self):
        from agent_core.context.compaction.shared import compaction_evidence_fallback_budgets, compaction_knob
        from agent_core.context.continuation.shared import relief_knob
        from agent_core.context.durable_memory import _memory_knob

        self.assertEqual(compaction_knob("compaction.min_recent_messages"), 20)
        self.assertEqual(compaction_evidence_fallback_budgets(), [90_000, 60_000, 40_000])
        self.assertEqual(relief_knob("context.relief_context_token_limit"), 200_000)
        self.assertEqual(_memory_knob("memory.retrieved_records"), 8)
        configureKnobs({
            "compaction": {"min_recent_messages": 5, "evidence_token_budget": 70_000},
            "context": {"relief_context_token_limit": 111_000},
            "memory": {"retrieved_records": 3},
        })
        self.assertEqual(compaction_knob("compaction.min_recent_messages"), 5)
        self.assertEqual(compaction_evidence_fallback_budgets(), [70_000, 60_000, 40_000])
        self.assertEqual(relief_knob("context.relief_context_token_limit"), 111_000)
        self.assertEqual(_memory_knob("memory.retrieved_records"), 3)


class ProviderKnobWiringTests(unittest.TestCase):
    """The E4 provider default-layer knobs (incl. the first float knobs)."""

    def tearDown(self):
        resetConfiguredKnobs()
        for name in list(os.environ):
            if name.startswith("AGENT_CORE_PROVIDER_"):
                del os.environ[name]

    def test_provider_defaults_and_float_ratios_read_knobs(self):
        from agent_core.providers._knob_reader import provider_knob
        from agent_core.providers.token_budget import resolve_compaction_threshold_tokens, resolve_provider_token_limits

        self.assertEqual(provider_knob("provider.openai_max_attempts"), 6)
        self.assertEqual(provider_knob("provider.anthropic_max_attempts"), 2)
        self.assertAlmostEqual(provider_knob("provider.compaction_preservation_ratio"), 0.1)
        self.assertEqual(resolve_provider_token_limits({})["maxContextLength"], 200_000)
        self.assertEqual(resolve_compaction_threshold_tokens({}), 200_000)

        configureKnobs({"provider": {
            "compaction_preservation_ratio": 0.2,
            "max_context_length": 128_000,
            "compaction_threshold_tokens": 90_000,
        }})
        self.assertAlmostEqual(provider_knob("provider.compaction_preservation_ratio"), 0.2)
        self.assertEqual(resolve_provider_token_limits({})["maxContextLength"], 128_000)
        self.assertEqual(resolve_compaction_threshold_tokens({}), 90_000)
        # Profile values still beat the knob default layer.
        self.assertEqual(resolve_provider_token_limits({"maxContextLength": 32_000})["maxContextLength"], 32_000)

        os.environ["AGENT_CORE_PROVIDER_COMPACTION_HEADROOM_RATIO"] = "0.5"
        configureKnobs({})
        self.assertAlmostEqual(provider_knob("provider.compaction_headroom_ratio"), 0.5)  # float env coercion


class PayloadKnobWiringTests(unittest.TestCase):
    """The E5 payload/web/image knobs drive their machinery."""

    def tearDown(self):
        resetConfiguredKnobs()

    def test_payload_web_and_image_defaults_read_knobs(self):
        from agent_core.acquisition.bounded_web_fetch import _knob as web_knob
        from agent_core.config.shared import default_max_image_input_bytes, default_max_image_input_count
        from agent_core.payloads.result_guard import non_artifact_tool_result_max_bytes

        self.assertEqual(non_artifact_tool_result_max_bytes(), 32_000)
        self.assertEqual(int(web_knob("web.fetch_total_timeout_ms")), 30_000)
        self.assertEqual(default_max_image_input_count(), 8)
        self.assertEqual(default_max_image_input_bytes(), 5 * 1024 * 1024)
        configureKnobs({
            "payloads": {"non_artifact_tool_result_max_bytes": 20_000},
            "web": {"fetch_total_timeout_ms": 12_000},
            "images": {"max_count": 2, "max_bytes": 1_000_000},
        })
        self.assertEqual(non_artifact_tool_result_max_bytes(), 20_000)
        self.assertEqual(int(web_knob("web.fetch_total_timeout_ms")), 12_000)
        self.assertEqual(default_max_image_input_count(), 2)
        self.assertEqual(default_max_image_input_bytes(), 1_000_000)


class ClosingWaveKnobWiringTests(unittest.TestCase):
    """The E6 tasks/agent/mcp/process knobs + legacy alias warnings."""

    def tearDown(self):
        resetConfiguredKnobs()
        for name in ("TESS_SUBAGENT_MAX_CONCURRENCY", "TESS_COMPACTION_SYNTHESIS_TIMEOUT_MS", "AGENT_CORE_MCP_BLOB_KEEP"):
            os.environ.pop(name, None)

    def test_monitor_agent_mcp_and_process_knobs_drive_machinery(self):
        from agent_core.authority.hooks import _knob as hooks_knob
        from agent_core.subagents.depth_policy import checkSubagentDepthAllowed
        from agent_core.tasks.monitors import defaultMonitorCheckIntervalMs

        self.assertEqual(defaultMonitorCheckIntervalMs(None), 5_000)
        self.assertEqual(int(hooks_knob("agent.hook_timeout_ms")), 60_000)
        deep_session = {"metadata": {"depth": 2}}
        self.assertFalse(checkSubagentDepthAllowed(deep_session, {})["allowed"])  # default depth 2
        configureKnobs({
            "tasks": {"monitor_check_interval_ms": 9_000},
            "agent": {"hook_timeout_ms": 1_000},
            "subagents": {"max_depth": 4},
        })
        self.assertEqual(defaultMonitorCheckIntervalMs(None), 9_000)
        self.assertEqual(int(hooks_knob("agent.hook_timeout_ms")), 1_000)
        self.assertTrue(checkSubagentDepthAllowed(deep_session, {})["allowed"])

    def test_mcp_blob_keep_env_absorption_is_canonical(self):
        from agent_core.mcp.blobs import _knob as mcp_knob

        self.assertEqual(int(mcp_knob("mcp.blob_keep")), 64)
        os.environ["AGENT_CORE_MCP_BLOB_KEEP"] = "5"  # the legacy name IS the knob env
        self.assertEqual(int(mcp_knob("mcp.blob_keep")), 5)

    def test_legacy_env_aliases_warn_once_naming_the_canonical_form(self):
        import io
        from contextlib import redirect_stderr

        from agent_core.config._knobs import _warned_legacy_env, warnLegacyEnvAlias

        _warned_legacy_env.discard("TESS_SUBAGENT_MAX_CONCURRENCY")
        os.environ["TESS_SUBAGENT_MAX_CONCURRENCY"] = "4"
        captured = io.StringIO()
        with redirect_stderr(captured):
            warnLegacyEnvAlias("TESS_SUBAGENT_MAX_CONCURRENCY", "subagents.max_live")
            warnLegacyEnvAlias("TESS_SUBAGENT_MAX_CONCURRENCY", "subagents.max_live")
        output = captured.getvalue()
        self.assertEqual(output.count("deprecated"), 1)  # once, ever
        self.assertIn("AGENT_CORE_SUBAGENTS_MAX_LIVE", output)


class PayloadClassKnobTests(unittest.TestCase):
    """E7: per-profile payloadClass + knob-backed payload budgets."""

    def tearDown(self):
        resetConfiguredKnobs()

    def test_profile_payload_class_beats_the_name_heuristic(self):
        from agent_core.payloads.tools import deriveToolPayloadClass

        sniffed = {"providerKind": "openai", "openaiBaseUrl": "https://api.example.test", "model": "deepseek-v4-flash"}
        self.assertEqual(deriveToolPayloadClass(sniffed), "small")  # heuristic fallback
        declared = {**sniffed, "providerProfile": {"payloadClass": "large"}}
        self.assertEqual(deriveToolPayloadClass(declared), "large")
        junk = {**sniffed, "providerProfile": {"payloadClass": "enormous"}}
        self.assertEqual(deriveToolPayloadClass(junk), "small")  # invalid values fall through

    def test_payload_budget_knobs_drive_policy_and_derived_line_caps(self):
        from agent_core.payloads.result_guard import getToolResultMaxBytes
        from agent_core.payloads.tools import getToolPayloadPolicy

        small = {"providerKind": "openai", "openaiBaseUrl": "https://x.test", "model": "deepseek-v4-flash"}
        policy = getToolPayloadPolicy(small)
        self.assertEqual(policy["textInlineBytes"], 8_000)
        self.assertEqual(policy["textInlineLines"], 400)
        self.assertEqual(getToolResultMaxBytes(small), 12_000)
        configureKnobs({"payloads": {"text_inline_bytes_small": 16_000, "result_final_bytes_small": 20_000}})
        policy = getToolPayloadPolicy(small)
        self.assertEqual(policy["textInlineBytes"], 16_000)
        self.assertEqual(policy["textInlineLines"], 800)  # derived: bytes // 20
        self.assertEqual(policy["textHeadLines"], 200)
        self.assertEqual(policy["textTailLines"], 400)
        self.assertEqual(getToolResultMaxBytes(small), 20_000)


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
