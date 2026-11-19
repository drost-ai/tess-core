"""A provider profile of kind google: the registry, config resolution and its credential."""
from __future__ import annotations

import asyncio
import os
import typing
from unittest import mock

from tess_core import ProviderRegistry, ProviderRegistryError
from tess_core.agent.request_integrity import STRICT_TOOL_PROTOCOL_PROVIDERS
from tess_core.agent.turn_session import resolve_effective_provider_turn_binding
from tess_core.config import (
    ensure_provider_for_model_selection,
    get_default_effort_for_provider,
    get_default_model_for_provider,
    list_providers,
    normalize_effort_for_model,
    normalize_provider_name,
    resolve_config,
)
from tess_core.config.providers import resolve_selected_provider_profile_name
from tess_core.config.shared import PROVIDER_KINDS, model_implies_image_input
from tess_core.context.token_accounting import measure_request_input_tokens
from tess_core.contracts.config import ProviderKind
from tess_core.payloads.image_budget import _estimate_measured_image_tokens
from tess_core.providers.google.thinking import google_thinking_levels, resolve_google_thinking_level
from tess_core.runtime.secret_provider import (
    assert_provider_config_secrets_resolved,
    resolve_provider_config_secrets,
)
from tess_core.runtime.secrets import RuntimeSecretError
from tests.support.tess_core_case import TessCoreTestCase


REGISTRY = """providerProfiles:
  gemini:
    kind: google
    defaultModel: gemini-4-hydrogen
    reasoningEffort: xhigh
    auth:
      kind: google_api_key
      envVar: SYNTHETIC_GEMINI_KEY
  flash:
    kind: google
    baseUrl: https://gemini.invalid/v1beta
    defaultModel: gemini-3.8-flash
    auth:
      kind: google_api_key
      envVar: SYNTHETIC_GEMINI_KEY
  console:
    kind: anthropic
    defaultModel: claude-test
    auth:
      kind: anthropic_api_key
      envVar: SYNTHETIC_KEY
"""


def _document(**overrides):
    record = {"kind": "google", "defaultModel": "gemini-4-hydrogen", "auth": {"kind": "google_api_key", "envVar": "SYNTHETIC_GEMINI_KEY"}}
    record.update(overrides)
    return {"providerProfiles": {"gemini": record}}


class GoogleProviderRegistryTests(TessCoreTestCase):
    def setUp(self):
        self.root, self.home, self.workspace, self.previous = self.temp_context()
        self.addCleanup(self._cleanup_temp_home)
        self.path = self.home / "providers.yaml"
        self.path.write_text(REGISTRY)
        self.registry = ProviderRegistry(self.path)

    def _cleanup_temp_home(self):
        previous_cwd = os.getcwd()
        try:
            os.chdir(self.root.name)
            self.restore_home(self.root, self.previous)
        finally:
            os.chdir(previous_cwd)

    def test_every_provider_kind_is_one_the_runtime_names(self):
        self.assertEqual(set(typing.get_args(ProviderKind)), set(PROVIDER_KINDS))
        self.assertEqual(list_providers(), list(PROVIDER_KINDS))
        self.assertEqual(STRICT_TOOL_PROTOCOL_PROVIDERS, set(PROVIDER_KINDS))
        for kind in PROVIDER_KINDS:
            self.assertTrue(get_default_model_for_provider(kind))
            self.assertTrue(get_default_effort_for_provider(kind))

    def test_a_google_profile_resolves_with_gemini_limits_and_image_input(self):
        profile = self.registry.resolve_profile("gemini")
        self.assertEqual(profile["kind"], "google")
        self.assertIsNone(profile["apiMode"])
        self.assertEqual(profile["maxContextLength"], 1_048_576)
        self.assertEqual(profile["maxOutputTokens"], 65_536)
        self.assertTrue(profile["capabilities"]["imageInput"])
        self.assertEqual(self.registry.resolve_profile("flash")["baseUrl"], "https://gemini.invalid/v1beta")

    def test_the_key_comes_back_in_its_own_field_and_only_from_the_named_variable(self):
        auth = self.registry.resolve_auth("gemini", environment={"SYNTHETIC_GEMINI_KEY": "fake-google"})
        self.assertEqual(auth, {"googleApiKey": "fake-google", "customHeaders": {}})
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "ambient", "GOOGLE_API_KEY": "ambient"}):
            with self.assertRaises(ProviderRegistryError) as error:
                self.registry.resolve_auth("gemini", environment={})
        self.assertNotIn("ambient", str(error.exception))

    def test_a_google_profile_takes_only_google_authentication(self):
        for auth in (
            {"kind": "openai_api_key", "envVar": "X"},
            {"kind": "anthropic_api_key", "envVar": "X"},
            {"kind": "custom_bearer", "envVar": "X"},
            {"kind": "chatgpt"},
            {"kind": "google_api_key"},
        ):
            with self.subTest(auth=auth), self.assertRaises(ProviderRegistryError):
                ProviderRegistry.from_document(_document(auth=auth))
        for kind in ("openai", "anthropic"):
            with self.subTest(kind=kind), self.assertRaises(ProviderRegistryError):
                ProviderRegistry.from_document(_document(kind=kind))
        self.assertEqual(ProviderRegistry.from_document(_document(auth={"kind": "none"})).resolve_auth("gemini"), {"customHeaders": {}})
        with self.assertRaises(ProviderRegistryError) as error:
            ProviderRegistry.from_document(_document(kind="vertex"))
        self.assertIn("anthropic, openai or google", str(error.exception))

    def test_a_key_written_into_the_registry_is_refused_without_being_echoed(self):
        for extra in ({"googleApiKey": "fake-do-not-echo"}, {"headers": {"x-goog-api-key": "fake-do-not-echo"}}, {"gemini_api_key": "fake-do-not-echo"}):
            with self.subTest(extra=extra), self.assertRaises(ProviderRegistryError) as error:
                ProviderRegistry.from_document(_document(**extra))
            self.assertNotIn("fake-do-not-echo", str(error.exception))

    def test_a_codex_auth_file_is_not_a_google_credential(self):
        with self.assertRaises(ProviderRegistryError):
            self.registry.resolve_auth("gemini", environment={}, codex_auth_path=self.home / "auth.json")

    def test_resolve_config_carries_the_key_as_a_secret_reference_and_no_other_credential(self):
        async def check():
            config = await resolve_config({"runtimeHome": str(self.home), "providerRegistry": self.registry, "provider": "flash", "settingSources": ""}, str(self.workspace))
            self.assertEqual((config["provider"], config["providerKind"], config["model"]), ("google", "google", "gemini-3.8-flash"))
            self.assertEqual(config["googleBaseUrl"], "https://gemini.invalid/v1beta")
            self.assertIsNone(config["openaiBaseUrl"])
            self.assertIsNone(config["anthropicBaseUrl"])
            self.assertIsNone(config["apiKey"])
            self.assertIsNone(config["authToken"])
            self.assertIsNone(config["openaiToken"])
            self.assertIsInstance(config["googleApiKey"], dict)
            self.assertNotIn("fake-google", repr(config))
            with self.assertRaises(RuntimeSecretError):
                assert_provider_config_secrets_resolved(config)
            resolved = await resolve_provider_config_secrets(config)
            self.assertEqual(resolved["googleApiKey"], "fake-google")
            assert_provider_config_secrets_resolved(resolved)
        with mock.patch.dict(os.environ, {"SYNTHETIC_GEMINI_KEY": "fake-google", "ANTHROPIC_API_KEY": "ambient-a", "OPENAI_API_KEY": "ambient-o", "ANTHROPIC_MODEL": "claude-ambient"}):
            asyncio.run(check())

    def test_the_default_effort_and_a_profile_effort_resolve_for_a_gemini_model(self):
        async def check(name):
            return await resolve_config({"runtimeHome": str(self.home), "providerRegistry": self.registry, "provider": name, "settingSources": ""}, str(self.workspace))
        with mock.patch.dict(os.environ, {"SYNTHETIC_GEMINI_KEY": "fake-google"}):
            self.assertEqual(asyncio.run(check("gemini"))["effort"], "xhigh")
            self.assertEqual(asyncio.run(check("flash"))["effort"], "high")

    def test_a_turn_binds_a_google_profile_to_its_own_base_url_only(self):
        profile = self.registry.resolve_profile("flash")
        config = {"providerRegistry": self.registry, "provider": "google", "providerProfileName": "flash", "providerProfile": profile, "openaiBaseUrl": "https://wrong.invalid", "anthropicBaseUrl": "https://wrong.invalid"}
        binding = resolve_effective_provider_turn_binding({"session": {"model": "gemini-3.8-flash", "providerProfileName": "flash"}, "config": config})["config"]
        self.assertEqual((binding["provider"], binding["googleBaseUrl"]), ("google", "https://gemini.invalid/v1beta"))
        self.assertIsNone(binding["openaiBaseUrl"])
        self.assertIsNone(binding["anthropicBaseUrl"])
        other = {"providerRegistry": self.registry, "provider": "anthropic", "providerProfileName": "console", "providerProfile": self.registry.resolve_profile("console"), "googleBaseUrl": "https://wrong.invalid"}
        self.assertIsNone(resolve_effective_provider_turn_binding({"session": {"model": "claude-test", "providerProfileName": "console"}, "config": other})["config"]["googleBaseUrl"])


class GoogleProviderSelectionTests(TessCoreTestCase):
    def test_provider_names_and_model_names_select_google(self):
        self.assertEqual(normalize_provider_name("Gemini"), "google")
        self.assertEqual(normalize_provider_name("google"), "google")
        self.assertEqual(ensure_provider_for_model_selection("openai", "gemini-4-neon"), "google")
        self.assertEqual(ensure_provider_for_model_selection("google", "some-tuned-model"), "google")

    def test_without_a_registry_a_google_request_needs_a_declared_google_profile(self):
        profiles = {"openai": {"kind": "openai"}, "mine": {"kind": "google"}}
        self.assertEqual(resolve_selected_provider_profile_name({"requestedName": "google", "profiles": profiles}), "mine")
        self.assertEqual(resolve_selected_provider_profile_name({"requestedModel": "gemini-4-neon", "profiles": profiles}), "mine")
        with self.assertRaises(ValueError) as error:
            resolve_selected_provider_profile_name({"requestedName": "gemini", "profiles": {"openai": {"kind": "openai"}}})
        self.assertIn("kind google", str(error.exception))

    def test_image_input_follows_the_model_name(self):
        self.assertTrue(model_implies_image_input("google", "gemini-4-hydrogen"))
        self.assertFalse(model_implies_image_input("google", "gemini-embedding-2"))
        self.assertFalse(model_implies_image_input("google", "lyria-3.5"))

    def test_thinking_levels_per_model(self):
        self.assertEqual(google_thinking_levels("gemini-4-hydrogen"), ("low", "medium", "high", "xhigh"))
        self.assertEqual(google_thinking_levels("models/gemini-4-carbon"), ("low", "medium", "high"))
        self.assertEqual(google_thinking_levels("gemini-3.1-flash-lite-image"), ("minimal", "high"))
        self.assertEqual(google_thinking_levels("gemini-9-unknown"), ())
        cases = [
            ("gemini-4-neon", "xhigh", "xhigh"), ("gemini-4-neon", "max", "xhigh"),
            ("gemini-4-carbon", "xhigh", "high"), ("gemini-4-carbon", "max", "high"),
            ("gemini-3-pro-preview", "medium", "low"), ("gemini-3.1-flash-lite-image", "low", "minimal"),
            ("gemini-3.6-flash", "low", "low"), ("gemini-9-unknown", "high", None),
            ("gemini-4-neon", None, None), ("gemini-4-neon", "bogus", None),
        ]
        for model, effort, expected in cases:
            with self.subTest(model=model, effort=effort):
                self.assertEqual(resolve_google_thinking_level(model, effort), expected)
                self.assertEqual(normalize_effort_for_model("google", model, effort), expected)

    def test_an_image_is_counted_the_way_gemini_counts_it(self):
        self.assertEqual(_estimate_measured_image_tokens({"width": 300, "height": 384, "decodedBytes": 1}, "google"), 258)
        self.assertEqual(_estimate_measured_image_tokens({"width": 1024, "height": 768, "decodedBytes": 1}, "google"), 258 * 2)
        self.assertEqual(_estimate_measured_image_tokens({"width": 2000, "height": 1600, "decodedBytes": 1}, "google"), 258 * 3 * 3)
        self.assertEqual(_estimate_measured_image_tokens({"decodedBytes": 10}, "google"), 258)

    def test_a_request_is_sized_by_estimate_and_labelled_google(self):
        request = {"model": "gemini-4-hydrogen", "system": "be brief", "messages": [{"role": "user", "content": "hello " * 400}], "tools": []}
        measured = asyncio.run(measure_request_input_tokens(config={"provider": "google", "providerProfile": {}}, request=request))
        self.assertEqual((measured["provider"], measured["source"], measured["exactCountFallback"]), ("google", "google_estimate", False))
        self.assertGreater(measured["baseInputTokens"], 300)
        self.assertEqual(measured["inputTokens"], measured["baseInputTokens"] + measured["safetyMarginTokens"])
        with self.assertRaises(Exception) as error:
            asyncio.run(measure_request_input_tokens(config={"provider": "google", "providerProfile": {"exactTokenCountRequired": True}}, request=request))
        self.assertIn("Exact token count", str(getattr(error.exception, "record", {}).get("rawMessage") or error.exception))
        with self.assertRaises(ValueError):
            asyncio.run(measure_request_input_tokens(config={"provider": "vertex"}, request=request))
