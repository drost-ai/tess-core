"""Strict provider resolution: temporary homes, synthetic secrets, no network."""
from __future__ import annotations

import asyncio
import json
import os
from unittest import mock

from tess_core import ProviderRegistry, ProviderRegistryError, SessionOptions, resolve_codex_auth
from tess_core.config import resolve_config
from tess_core.runtime.secret_provider import resolve_provider_config_secrets
from tests.support.tess_core_case import TessCoreTestCase


REGISTRY = """providerProfiles:
  custom:
    kind: openai
    apiMode: chat_completions
    baseUrl: https://provider.invalid/v1
    defaultModel: local-test
    auth:
      kind: custom_bearer
      envVar: SYNTHETIC_TOKEN
  console:
    kind: anthropic
    defaultModel: claude-test
    auth:
      kind: anthropic_api_key
      envVar: SYNTHETIC_KEY
  bearer:
    kind: anthropic
    defaultModel: claude-test
    auth:
      kind: anthropic_auth_token
      envVar: SYNTHETIC_BEARER
"""


class ProviderRegistryTests(TessCoreTestCase):
    def setUp(self):
        self.root, self.home, self.workspace, self.previous = self.temp_context()
        self.addCleanup(self._cleanup_temp_home)
        self.path = self.home / "providers.yaml"
        self.path.write_text(REGISTRY)
        self.registry = ProviderRegistry(self.path)

    def _cleanup_temp_home(self):
        # rmtree uses dir_fd-relative opens; anchor audit resolution inside the
        # owned temporary root, without widening the test guard allowlist.
        previous_cwd = os.getcwd()
        try:
            os.chdir(self.root.name)
            self.restore_home(self.root, self.previous)
        finally:
            os.chdir(previous_cwd)

    def test_mixed_case_identifiers_preserve_exact_lookup(self):
        self.path.write_text(REGISTRY.replace("  custom:", "  Model-27B:"))
        registry = ProviderRegistry(self.path)
        self.assertEqual(registry.resolve_profile("Model-27B")["defaultModel"], "local-test")
        with self.assertRaises(ProviderRegistryError):
            registry.resolve_profile("model-27b")

    def test_invalid_identifiers_remain_rejected(self):
        for name in ("bad/name", "bad name", "../profile", "mödél"):
            with self.subTest(name=name):
                self.path.write_text(REGISTRY.replace("  custom:", f"  {name}:"))
                with self.assertRaises(ProviderRegistryError):
                    ProviderRegistry(self.path)

    def test_missing_path_unknown_profile_and_missing_name_fail_closed(self):
        with self.assertRaises(ProviderRegistryError):
            ProviderRegistry(self.home / "missing.yaml")
        for name in (None, "", "openai", "sk-secret-never-echo"):
            with self.subTest(name=name), self.assertRaises(ProviderRegistryError) as error:
                self.registry.resolve_profile(name)
            self.assertNotIn("sk-secret-never-echo", str(error.exception))

    def test_auth_exact_environment_and_transports(self):
        env = {"SYNTHETIC_TOKEN": "fake-openai", "SYNTHETIC_KEY": "fake-key", "SYNTHETIC_BEARER": "fake-bearer"}
        self.assertEqual(self.registry.resolve_auth("custom", environment=env)["openaiToken"], "fake-openai")
        self.assertEqual(self.registry.resolve_auth("console", environment=env)["apiKey"], "fake-key")
        self.assertEqual(self.registry.resolve_auth("bearer", environment=env)["authToken"], "fake-bearer")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "ambient", "SYNTHETIC_TOKEN": "ambient-custom"}):
            with self.assertRaises(ProviderRegistryError):
                self.registry.resolve_auth("custom", environment={})

    def test_profiles_do_not_mutate_registry(self):
        profile = self.registry.resolve_profile("custom")
        profile["auth"]["envVar"] = "OTHER"
        raw = self.registry.profiles
        raw.clear()
        self.assertEqual(self.registry.resolve_profile("custom")["auth"]["envVar"], "SYNTHETIC_TOKEN")

    def test_parse_error_redacts_values(self):
        self.path.write_text('providerProfiles:\n  custom: ["sk-secret"]\n')
        with self.assertRaises(ProviderRegistryError) as error:
            ProviderRegistry(self.path)
        self.assertNotIn("sk-secret", str(error.exception))
        self.assertIn("YAML", str(error.exception))

    def test_resolve_config_ignores_legacy_auth_and_ambient_selection(self):
        async def check():
            config = await resolve_config({"runtimeHome": str(self.home), "providerRegistry": self.registry, "provider": "custom", "settingSources": ""}, str(self.workspace))
            resolved = await resolve_provider_config_secrets(config)
            self.assertEqual(resolved["openaiToken"], "fake-token")
            self.assertEqual(config["providerProfileName"], "custom")
            self.assertEqual(config["openaiBaseUrl"], "https://provider.invalid/v1")
            self.assertEqual(set(config["providerProfiles"]), {"custom", "console", "bearer"})
        with mock.patch.dict(os.environ, {"SYNTHETIC_TOKEN": "fake-token", "TESS_PROVIDER": "unknown", "TESS_MODEL": "gpt-ambient", "OPENAI_BASE_URL": "https://wrong.invalid", "ANTHROPIC_CUSTOM_HEADERS": "Authorization: fake-ambient"}), mock.patch("tess_core.config.resolution.load_auth", side_effect=AssertionError("legacy auth read")), mock.patch("tess_core.config.resolution.load_codex_auth_token", side_effect=AssertionError("ambient codex read")), mock.patch("tess_core.config.resolution._resolve_api_key_from_helper", side_effect=AssertionError("helper called")):
            asyncio.run(check())

    def test_explicit_yaml_engine_settings_preserved_for_memory_and_compaction(self):
        settings = self.home / "config.yaml"
        settings.write_text("durableMemory:\n  enabled: false\ncompactionModel: local-compact\ncompactionProviderProfile: custom\n")
        async def check():
            config = await resolve_config({"runtimeHome": str(self.home), "providerRegistry": self.registry, "provider": "custom", "settings": str(settings), "settingSources": ""}, str(self.workspace))
            self.assertEqual(config["durableMemory"], {"enabled": False})
            self.assertEqual(config["compactionModel"], "local-compact")
            self.assertEqual(config["compactionProviderProfile"]["profileName"], "custom")
        with mock.patch.dict(os.environ, {"SYNTHETIC_TOKEN": "fake-token"}):
            asyncio.run(check())

    def test_unbound_compaction_runs_on_the_session_profile_not_a_fixed_model(self):
        from tess_core.context.compaction_transport import create_default_compaction_model_client, resolve_compaction_execution_plan
        async def check():
            config = await resolve_config({"runtimeHome": str(self.home), "providerRegistry": self.registry, "provider": "custom", "settingSources": ""}, str(self.workspace))
            plan = resolve_compaction_execution_plan({"config": config, "session": {"provider": "openai", "model": "local-test"}})
            for purpose in ("synthesis", "verification"):
                self.assertEqual((plan[purpose]["providerProfileName"], plan[purpose]["model"], plan[purpose]["selectionSource"]), ("custom", "local-test", "session"))
            sender = mock.AsyncMock(return_value={"content": [{"type": "text", "text": "summary"}]})
            result = await create_default_compaction_model_client({"openAiRequestSender": sender})["complete"]({"config": config, "model": plan["synthesis"], "system": "s", "prompt": "p"})
            sent = sender.call_args.args[0]
            self.assertEqual((result["text"], sent["request"]["model"], sent["token"], sent["baseUrl"], sent["mode"]), ("summary", "local-test", "fake-token", "https://provider.invalid/v1", "chat_completions"))
        with mock.patch.dict(os.environ, {"SYNTHETIC_TOKEN": "fake-token"}):
            asyncio.run(check())

    def test_explicit_registry_requires_explicit_profile_even_with_ambient(self):
        with mock.patch.dict(os.environ, {"TESS_PROVIDER": "custom"}):
            with self.assertRaises(ProviderRegistryError):
                asyncio.run(resolve_config({"runtimeHome": str(self.home), "providerRegistry": str(self.path), "settingSources": ""}, str(self.workspace)))

    def test_session_options_forward_registry(self):
        options = SessionOptions(provider="custom", provider_registry=self.registry)
        self.assertIs(options.config_overrides(str(self.home))["providerRegistry"], self.registry)

    def test_explicit_codex_auth_file(self):
        path = self.home / "fake-auth.json"
        path.write_text(json.dumps({"tokens": {"access_token": "fake-access"}}))
        self.assertEqual(asyncio.run(resolve_codex_auth(path)), "fake-access")
        path.write_text('{"secret": "sk-must-not-echo"}')
        with self.assertRaises(ProviderRegistryError) as error:
            asyncio.run(resolve_codex_auth(path))
        self.assertNotIn("sk-must-not-echo", str(error.exception))

    def test_turn_binding_never_infers_other_provider_from_model(self):
        from tess_core.agent.turn_session import resolve_effective_provider_turn_binding
        config = {"providerRegistry": self.registry, "provider": "openai", "providerProfileName": "custom", "providerProfile": self.registry.resolve_profile("custom")}
        binding = resolve_effective_provider_turn_binding({"session": {"model": "claude-looking-model", "providerProfileName": "custom"}, "config": config})
        self.assertEqual(binding["config"]["provider"], "openai")
        with self.assertRaises(ProviderRegistryError):
            resolve_effective_provider_turn_binding({"session": {"model": "test", "providerProfileName": "missing"}, "config": config})


    def test_explicit_json_compatibility_and_schema_validation(self):
        path = self.home / "legacy-explicit.json"
        path.write_text(json.dumps({"providerProfiles": self.registry.profiles, "schema_version": 1}))
        self.assertEqual(ProviderRegistry(path).resolve_profile("custom")["kind"], "openai")
        for version in (2, True, "1"):
            path.write_text(json.dumps({"providerProfiles": self.registry.profiles, "schema_version": version}))
            with self.assertRaises(ProviderRegistryError):
                ProviderRegistry(path)

    def test_explicit_no_sources_never_loads_ambient_executable_configuration(self):
        from tess_core.authority.executable_source_policy import parse_setting_sources, load_selected_mcp_servers, load_selected_plugin_records
        from tess_core.authority.permission_rules import load_permission_rules
        from tess_core.authority.hooks import load_hooks
        self.assertEqual(parse_setting_sources(""), [])
        self.assertEqual(parse_setting_sources([]), [])
        self.assertEqual(parse_setting_sources(None), ["user", "project", "local"])
        async def check():
            with mock.patch("tess_core.authority.executable_source_policy._load_scoped_mcp_records", side_effect=AssertionError("ambient MCP")), mock.patch("tess_core.authority.executable_source_policy.load_plugins", side_effect=AssertionError("ambient plugins")):
                self.assertEqual(await load_selected_mcp_servers(cwd=str(self.workspace), sources=[]), [])
                self.assertEqual(await load_selected_plugin_records(cwd=str(self.workspace), sources=[]), [])
            with mock.patch("tess_core.authority.permission_rules.load_settings", side_effect=AssertionError("ambient permissions")):
                self.assertEqual(await load_permission_rules(cwd=str(self.workspace), sources=[]), [])
            with mock.patch("tess_core.authority.hooks.load_settings", side_effect=AssertionError("ambient hooks")):
                self.assertEqual(await load_hooks(cwd=str(self.workspace), sources=[]), [])
        asyncio.run(check())


    def test_runtime_registry_inherited_and_persisted_for_task_runners(self):
        from tess_core import start_runtime
        from tess_core.storage import load_session
        from tess_core.tasks.runners.main_session import _session_config_options as main_options
        from tess_core.tasks.runners.subagent import _session_config_options as child_options
        async def check():
            runtime = await start_runtime(str(self.home), cwd=str(self.workspace), daemon_enabled=False, provider_registry=self.path, session_options=lambda *_: SessionOptions(provider="custom", extra={"settingSources": ""}))
            try:
                session = await runtime.sessions.open()
                record = await load_session(session.id)
                self.assertEqual(record["metadata"]["agentCoreProviderRegistryPath"], str(self.path))
                for options in (main_options(record, str(self.home)), child_options(record, str(self.home))):
                    self.assertEqual(options["providerRegistry"], str(self.path))
                    self.assertEqual(options["settingSources"], [])
            finally:
                await runtime.stop()
        with mock.patch.dict(os.environ, {"SYNTHETIC_TOKEN": "fake-runtime-token"}):
            asyncio.run(check())


    def test_existing_session_keeps_its_bound_profile_when_the_opener_selection_changes(self):
        from tess_core import start_runtime
        from tess_core.agent.turn_session import resolve_effective_provider_turn_binding
        from tess_core.storage import load_session
        self.path.write_text(REGISTRY + """  renamed:
    kind: openai
    apiMode: chat_completions
    baseUrl: https://provider.invalid/v1
    defaultModel: renamed-test
    auth:
      kind: custom_bearer
      envVar: SYNTHETIC_TOKEN
""")
        selection = {"provider": "custom"}
        async def check():
            runtime = await start_runtime(str(self.home), cwd=str(self.workspace), daemon_enabled=False, provider_registry=self.path, session_options=lambda *_: SessionOptions(provider=selection["provider"], extra={"settingSources": ""}))
            try:
                existing = await runtime.sessions.open()
                await runtime.sessions.close(existing.id)
                selection["provider"] = "renamed"
                await runtime.sessions.open(existing.id)
                live = runtime.sessions._live[existing.id]
                self.assertEqual((live.config["providerProfileName"], live.config["model"]), ("custom", "local-test"))
                # The turn binding's guard accepts it: config and session agree.
                resolve_effective_provider_turn_binding({"session": await load_session(existing.id), "config": live.config})
                fresh = await runtime.sessions.open()
                record = await load_session(fresh.id)
                self.assertEqual((record["providerProfileName"], record["model"]), ("renamed", "renamed-test"))
            finally:
                await runtime.stop()
        with mock.patch.dict(os.environ, {"SYNTHETIC_TOKEN": "fake-runtime-token"}):
            asyncio.run(check())


    def test_anthropic_transport_headers_are_preserved(self):
        from tess_core.providers.anthropic.payloads import build_anthropic_headers
        key_headers = build_anthropic_headers(self.registry.resolve_auth("console", environment={"SYNTHETIC_KEY": "fake-key"}))
        bearer_headers = build_anthropic_headers(self.registry.resolve_auth("bearer", environment={"SYNTHETIC_BEARER": "fake-token"}))
        self.assertEqual(key_headers["x-api-key"], "fake-key")
        self.assertNotIn("authorization", key_headers)
        self.assertEqual(bearer_headers["authorization"], "Bearer fake-token")
        self.assertNotIn("x-api-key", bearer_headers)

    def test_public_secret_normalization_preserves_setup_reference(self):
        from tess_core import resolve_provider_secret
        from tess_core.storage import configure_runtime_home
        configure_runtime_home(str(self.home))
        async def check():
            setup = await resolve_provider_secret("fake-setup", purpose="provider_setup_token", idempotency_scope="test-setup")
            oauth = await resolve_provider_secret(setup, purpose="provider_oauth_token", idempotency_scope="test-oauth")
            self.assertEqual(oauth, setup)
            self.assertEqual(oauth["purpose"], "provider_setup_token")
        asyncio.run(check())


    def test_registry_rejects_embedded_credentials_with_redacted_errors(self):
        path = self.home / "invalid.json"
        for extra in ({"apiKey": "fake-do-not-echo"}, {"authToken": "fake-do-not-echo"}, {"headers": {"Authorization": "Bearer fake-do-not-echo"}}, {"headers": {"x-api-key": "fake-do-not-echo"}}, {"requestOptions": {"access_token": "fake-do-not-echo"}}, {"auth": {"kind": "custom_bearer", "envVar": "SYNTHETIC_TOKEN", "value": "fake-do-not-echo"}}):
            path.write_text(json.dumps({"profiles": {"custom": {**self.registry.profiles["custom"], **extra}}}))
            with self.subTest(extra=extra), self.assertRaises(ProviderRegistryError) as error:
                ProviderRegistry(path)
            self.assertNotIn("fake-do-not-echo", str(error.exception))
        record = self.registry.profiles["console"]
        record["headers"] = {"anthropic-beta": "fake-beta"}
        path.write_text(json.dumps({"profiles": {"console": record}}))
        self.assertEqual(ProviderRegistry(path).resolve_profile("console")["headers"], {"anthropic-beta": "fake-beta"})


    def test_explicit_codex_pin_beats_environment_and_is_required_without_env(self):
        path = self.home / "pinned-auth.json"
        path.write_text(json.dumps({"tokens": {"access_token": "fake-pinned"}}))
        self.assertEqual(self.registry.resolve_auth("custom", environment={"SYNTHETIC_TOKEN": "fake-wrong-account"}, codex_auth_path=path)["openaiToken"], "fake-pinned")
        with self.assertRaises(ProviderRegistryError):
            self.registry.resolve_auth("custom", environment={"SYNTHETIC_TOKEN": "fake-wrong-account"}, codex_auth_path=self.home / "missing.json")
        record = self.registry.profiles["custom"]
        record["auth"] = {"kind": "openai_api_key_or_codex"}
        registry_path = self.home / "codex-registry.json"
        registry_path.write_text(json.dumps({"profiles": {"pinned": record}}))
        registry = ProviderRegistry(registry_path)
        with self.assertRaises(ProviderRegistryError):
            registry.resolve_auth("pinned", environment={})
        async def check():
            config = await resolve_config({"runtimeHome": str(self.home), "providerRegistry": registry, "provider": "pinned", "providerAuthPath": str(path), "settingSources": "", "compactionProviderProfile": "pinned"}, str(self.workspace))
            resolved = await resolve_provider_config_secrets(config)
            self.assertEqual(resolved["openaiToken"], "fake-pinned")
        asyncio.run(check())


    def test_explicit_noauth_reaches_wire_without_dummy_or_ambient_credentials(self):
        from tess_core.agent.providers import _create_provider
        from tess_core.providers.openai.transport import _create_openai_client
        from tess_core.providers.anthropic.payloads import build_anthropic_headers
        profile = self.registry.profiles["custom"]
        profile["auth"] = {"kind": "none"}
        path = self.home / "local.json"
        path.write_text(json.dumps({"profiles": {"local": profile}}))
        registry = ProviderRegistry(path)
        async def check():
            config = await resolve_config({"runtimeHome": str(self.home), "providerRegistry": registry, "provider": "local", "settingSources": ""}, str(self.workspace))
            self.assertIsNone(config["openaiToken"])
            with mock.patch("tess_core.agent.providers.send_openai_request", new_callable=mock.AsyncMock, return_value={"synthetic": True}) as send:
                result = await _create_provider(config, {})["send"]({"model": "local-test"})
                self.assertEqual(result, {"synthetic": True})
                self.assertIsNone(send.call_args.kwargs["token"])
            args = {"token": None, "config": config}
            client = _create_openai_client(args, "https://local.invalid/v1")
            _, _, headers = client._prepare_request("/chat/completions", {"model": "local-test"})
            self.assertNotIn("authorization", {key.lower() for key in headers})
            with self.assertRaisesRegex(RuntimeError, "wire transport"):
                _create_openai_client({**args, "config": {**config, "openaiTransport": "sdk"}}, "https://local.invalid/v1")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "fake-ambient-not-used"}):
            asyncio.run(check())
        headers = build_anthropic_headers({"providerProfile": {"auth": {"kind": "none"}}})
        self.assertNotIn("authorization", headers)
        self.assertNotIn("x-api-key", headers)
