from __future__ import annotations

from tests.support.agent_core_case import *
from agent_core.runtime.secret_provider import resolve_compaction_provider_secrets, resolve_provider_config_secrets
from agent_core.runtime.secrets import is_runtime_secret_ref


class ConfigProfileTests(AgentCoreTestCase):
    def test_built_in_provider_profiles(self):
        profiles = core.resolveProviderProfiles(None)
        self.assertEqual(core.listProviderProfiles(profiles), ["anthropic-claude-code", "anthropic-console", "codex", "openai"])
        self.assertEqual(profiles["codex"]["defaultModel"], "gpt-5.4")

    def test_resolve_config_custom_profile(self):
        root, home, workspace, previous = self.temp_context()
        prev_key = os.environ.get("DEEPINFRA_API_KEY")
        os.environ["DEEPINFRA_API_KEY"] = "deepinfra-test-key"
        try:
            (home / "settings.json").write_text(
                json.dumps(
                    {
                        "provider": "deepinfra",
                        "providerProfiles": {
                            "deepinfra": {
                                "kind": "openai",
                                "apiMode": "chat_completions",
                                "baseUrl": "https://api.deepinfra.com/v1/openai",
                                "defaultModel": "zai-org/GLM-5",
                                "auth": {"kind": "custom_bearer", "envVar": "DEEPINFRA_API_KEY"},
                            }
                        },
                    }
                )
            )
            config = run(core.resolveConfig({}, str(workspace)))
            self.assertEqual(config["providerProfileName"], "deepinfra")
            self.assertEqual(config["provider"], "openai")
            self.assertEqual(config["openaiApiMode"], "chat_completions")
            self.assertEqual(config["openaiBaseUrl"], "https://api.deepinfra.com/v1/openai")
            self.assertTrue(is_runtime_secret_ref(config["openaiToken"]))
            self.assertEqual(run(resolve_provider_config_secrets(config))["openaiToken"], "deepinfra-test-key")
            self.assertEqual(config["model"], "zai-org/GLM-5")
        finally:
            if prev_key is None:
                os.environ.pop("DEEPINFRA_API_KEY", None)
            else:
                os.environ["DEEPINFRA_API_KEY"] = prev_key
            self.restore_home(root, previous)

    def test_resolve_config_preserves_web_search_settings(self):
        root, home, workspace, previous = self.temp_context()
        try:
            (home / "settings.json").write_text(
                json.dumps(
                    {
                        "webSearch": {
                            "provider": "exa",
                            "exaApiKeyEnv": "EXA_API_KEY",
                            "urlAllowlist": ["openai.com"],
                        },
                        "env": {"EXA_API_KEY": "settings-exa-key", "TESS_WEB_SEARCH_PROVIDER": "exa"},
                    }
                ),
                encoding="utf-8",
            )
            config = run(core.resolveConfig({"runtimeHome": str(home)}, str(workspace)))
            self.assertEqual(
                config["webSearch"],
                {
                    "provider": "exa",
                    "exaApiKeyEnv": "EXA_API_KEY",
                    "urlAllowlist": ["openai.com"],
                },
            )
            self.assertEqual(config["settingsEnv"]["EXA_API_KEY"], "settings-exa-key")
            self.assertEqual(config["settingsEnv"]["TESS_WEB_SEARCH_PROVIDER"], "exa")
        finally:
            self.restore_home(root, previous)

    def test_system_prompt_precedence_options_file_product_builtin(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"runtimeHome": str(home), "defaultSystemPrompt": "product prompt"}, str(workspace)))
            self.assertEqual(config["systemPrompt"], "product prompt")

            (home / "settings").mkdir()
            (home / "settings" / "system_prompt.md").write_text("file prompt\n", encoding="utf-8")
            file_config = run(core.resolveConfig({"runtimeHome": str(home), "defaultSystemPrompt": "product prompt"}, str(workspace)))
            self.assertEqual(file_config["systemPrompt"], "file prompt")

            explicit_config = run(core.resolveConfig({"runtimeHome": str(home), "systemPrompt": "explicit prompt", "defaultSystemPrompt": "product prompt"}, str(workspace)))
            self.assertEqual(explicit_config["systemPrompt"], "explicit prompt")
        finally:
            self.restore_home(root, previous)

    def test_settings_record_prompt_keys_are_not_consulted(self):
        root, home, workspace, previous = self.temp_context()
        try:
            (home / "settings.json").write_text(
                json.dumps({"systemPrompt": "legacy prompt", "defaultSystemPrompt": "legacy default", "appendSystemPrompt": "legacy append"}),
                encoding="utf-8",
            )
            (home / "settings").mkdir()
            (home / "settings" / "config.yaml").write_text("systemPrompt: yaml prompt\n", encoding="utf-8")
            config = run(core.resolveConfig({"runtimeHome": str(home)}, str(workspace)))
            self.assertEqual(config["systemPrompt"], core.buildDefaultSystemPrompt())
            self.assertIsNone(config["appendSystemPrompt"])
        finally:
            self.restore_home(root, previous)

    def test_explicit_profile_uses_own_default_model_not_top_level_model(self):
        root, home, workspace, previous = self.temp_context()
        previous_env = {
            "TESS_MODEL": os.environ.get("TESS_MODEL"),
            "OPENAI_MODEL": os.environ.get("OPENAI_MODEL"),
        }
        os.environ.pop("TESS_MODEL", None)
        os.environ["OPENAI_MODEL"] = "gpt-5.5"
        try:
            (home / "settings.json").write_text(
                json.dumps(
                    {
                        "provider": "openai-codex",
                        "model": "gpt-5.5",
                        "providerProfiles": {
                            "openai-codex": {
                                "kind": "openai",
                                "apiMode": "codex",
                                "defaultModel": "gpt-5.5",
                            },
                            "deepseek": {
                                "kind": "openai",
                                "apiMode": "chat_completions",
                                "baseUrl": "https://api.deepseek.com",
                                "defaultModel": "deepseek-v4-pro",
                                "auth": {"kind": "custom_bearer", "envVar": "DEEPSEEK_API_KEY"},
                                "requestOptions": {"thinking": {"type": "enabled"}},
                                "capabilities": {"vision": False, "tools": True},
                            },
                        },
                    }
                )
            )
            config = run(core.resolveConfig({"provider": "deepseek"}, str(workspace)))
            self.assertEqual(config["providerProfileName"], "deepseek")
            self.assertEqual(config["model"], "deepseek-v4-pro")
            self.assertEqual(config["openaiBaseUrl"], "https://api.deepseek.com")
            self.assertEqual(config["providerProfile"]["requestOptions"], {"thinking": {"type": "enabled"}})
            self.assertEqual(config["providerProfile"]["capabilities"]["tools"], True)
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self.restore_home(root, previous)

    def test_provider_profile_token_limits_timeouts_and_compaction_binding(self):
        root, home, workspace, previous = self.temp_context()
        prev_key = os.environ.get("LOCAL_API_KEY")
        os.environ["LOCAL_API_KEY"] = "local-token"
        try:
            (home / "settings.json").write_text(
                json.dumps(
                    {
                        "provider": "local-small",
                        "compactionProviderProfile": "local-small",
                        "providerProfiles": {
                            "local-small": {
                                "kind": "openai",
                                "apiMode": "chat_completions",
                                "baseUrl": "http://127.0.0.1:8080/v1",
                                "defaultModel": "llamacpp",
                                "auth": {"kind": "custom_bearer", "envVar": "LOCAL_API_KEY"},
                                "maxContextLength": 32_000,
                                "maxOutputTokens": 4_096,
                                "compactionThresholdTokens": 28_000,
                                "compactionOutputTokens": 3_000,
                                "requestStartTimeoutMs": 7_000,
                                "streamIdleTimeoutMs": 8_000,
                                "tokenCountTimeoutMs": 15_000,
                                "totalRequestTimeoutMs": 600_000,
                                "exactTokenCountRequired": True,
                            }
                        },
                    }
                )
            )
            config = run(core.resolveConfig({}, str(workspace)))
            profile = config["providerProfile"]
            self.assertEqual(profile["maxContextLength"], 32_000)
            self.assertEqual(profile["maxOutputTokens"], 4_096)
            self.assertEqual(profile["compactionThresholdTokens"], 28_000)
            self.assertEqual(profile["compactionOutputTokens"], 3_000)
            self.assertEqual(profile["requestStartTimeoutMs"], 7_000)
            self.assertEqual(profile["streamIdleTimeoutMs"], 8_000)
            self.assertEqual(profile["tokenCountTimeoutMs"], 15_000)
            self.assertEqual(profile["totalRequestTimeoutMs"], 600_000)
            self.assertTrue(profile["exactTokenCountRequired"])
            self.assertEqual(config["compactionProviderProfile"]["profileName"], "local-small")
            self.assertTrue(is_runtime_secret_ref(config["compactionProviderProfile"]["openaiToken"]))
            self.assertEqual(
                run(resolve_compaction_provider_secrets(config["compactionProviderProfile"]))["openaiToken"],
                "local-token",
            )
        finally:
            if prev_key is None:
                os.environ.pop("LOCAL_API_KEY", None)
            else:
                os.environ["LOCAL_API_KEY"] = prev_key
            self.restore_home(root, previous)

    def test_direct_compaction_options_override_settings_and_bind_codex_auth(self):
        root, home, workspace, previous = self.temp_context()
        try:
            codex_auth_path = home / "codex-auth.json"
            codex_auth_path.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {"access_token": "codex-access-token"},
                    }
                ),
                encoding="utf-8",
            )
            (home / "settings.json").write_text(
                json.dumps(
                    {
                        "openaiCodexAuthPath": str(codex_auth_path),
                        "compactionModel": "gpt-5.3-codex-spark",
                        "compactionEffort": "high",
                    }
                ),
                encoding="utf-8",
            )
            config = run(
                core.resolveConfig(
                    {
                        "runtimeHome": str(home),
                        "compactionProvider": "openai",
                        "compactionProviderProfile": "codex",
                        "compactionModel": "gpt-5.6-terra",
                        "compactionEffort": "medium",
                        "compactionVerificationProvider": "openai",
                        "compactionVerificationProviderProfile": "codex",
                        "compactionVerificationModel": "gpt-5.6-terra",
                        "compactionVerificationEffort": "medium",
                    },
                    str(workspace),
                )
            )
            self.assertEqual(config["compactionProvider"], "openai")
            self.assertEqual(config["compactionModel"], "gpt-5.6-terra")
            self.assertEqual(config["compactionEffort"], "medium")
            self.assertEqual(config["compactionProviderProfile"]["profileName"], "codex")
            self.assertTrue(is_runtime_secret_ref(config["compactionProviderProfile"]["openaiToken"]))
            self.assertEqual(config["compactionVerificationModel"], "gpt-5.6-terra")
            self.assertEqual(config["compactionVerificationEffort"], "medium")
            self.assertEqual(
                config["compactionVerificationProviderProfile"]["profileName"],
                "codex",
            )
        finally:
            self.restore_home(root, previous)

    def test_provider_profile_effort_level_is_used_as_route_default(self):
        root, home, workspace, previous = self.temp_context()
        try:
            (home / "settings.json").write_text(
                json.dumps(
                    {
                        "provider": "anthropic-oauth",
                        "providerProfiles": {
                            "anthropic-oauth": {
                                "kind": "anthropic",
                                "defaultModel": "claude-opus-4-7",
                                "auth": {"kind": "anthropic_auth_token", "envVar": "ANTHROPIC_AUTH_TOKEN"},
                                "effortLevel": "high",
                            }
                        },
                    }
                )
            )
            config = run(core.resolveConfig({}, str(workspace)))
            self.assertEqual(config["providerProfileName"], "anthropic-oauth")
            self.assertEqual(config["model"], "claude-opus-4-7")
            self.assertEqual(config["effort"], "high")
        finally:
            self.restore_home(root, previous)

    def test_resolve_config_anthropic_auth_separation_and_alt_screen(self):
        root, home, workspace, previous = self.temp_context()
        prev_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        prev_key = os.environ.get("ANTHROPIC_API_KEY")
        try:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-ant-o-test-token"
            token_config = run(core.resolveConfig({"provider": "anthropic"}, str(workspace)))
            self.assertEqual(token_config["providerProfileName"], "anthropic-claude-code")
            self.assertTrue(is_runtime_secret_ref(token_config["authToken"]))
            self.assertEqual(run(resolve_provider_config_secrets(token_config))["authToken"], "sk-ant-o-test-token")
            self.assertIsNone(token_config["apiKey"])

            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api-test-key"
            key_config = run(core.resolveConfig({"provider": "anthropic"}, str(workspace)))
            self.assertEqual(key_config["providerProfileName"], "anthropic-console")
            self.assertTrue(is_runtime_secret_ref(key_config["apiKey"]))
            self.assertEqual(run(resolve_provider_config_secrets(key_config))["apiKey"], "sk-ant-api-test-key")
            self.assertIsNone(key_config["authToken"])

            self.assertEqual(run(core.resolveConfig({"altScreen": False}, str(workspace)))["screenMode"], "inline")
            self.assertEqual(run(core.resolveConfig({"noAltScreen": True}, str(workspace)))["screenMode"], "inline")
        finally:
            if prev_token is None:
                os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            else:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = prev_token
            if prev_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev_key
            self.restore_home(root, previous)

    def test_resolve_config_stored_anthropic_profile_credentials(self):
        root, home, workspace, previous = self.temp_context()
        prev_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        prev_key = os.environ.get("ANTHROPIC_API_KEY")
        try:
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)
            (home / "auth.json").write_text(json.dumps({"provider": "claudeai", "apiKey": "sk-ant-o-stored-token"}))
            token_config = run(core.resolveConfig({"provider": "anthropic"}, str(workspace)))
            self.assertEqual(token_config["providerProfileName"], "anthropic-claude-code")
            self.assertTrue(is_runtime_secret_ref(token_config["authToken"]))
            self.assertEqual(run(resolve_provider_config_secrets(token_config))["authToken"], "sk-ant-o-stored-token")
            self.assertIsNone(token_config["apiKey"])

            (home / "auth.json").write_text(json.dumps({"provider": "console", "apiKey": "sk-ant-api-stored-key"}))
            key_config = run(core.resolveConfig({"provider": "anthropic"}, str(workspace)))
            self.assertEqual(key_config["providerProfileName"], "anthropic-console")
            self.assertTrue(is_runtime_secret_ref(key_config["apiKey"]))
            self.assertEqual(run(resolve_provider_config_secrets(key_config))["apiKey"], "sk-ant-api-stored-key")
            self.assertIsNone(key_config["authToken"])
        finally:
            if prev_token is None:
                os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            else:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = prev_token
            if prev_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev_key
            self.restore_home(root, previous)

    def test_resolve_config_prefers_codex_oauth_for_codex_profile(self):
        root, home, workspace, previous = self.temp_context()
        prev_openai = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "openai-api-key-should-not-win"
        try:
            codex_auth_path = home / "codex-auth.json"
            codex_auth_path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "codex-access-token"}}))
            (home / "settings.json").write_text(json.dumps({"openaiCodexAuthPath": str(codex_auth_path)}))
            config = run(core.resolveConfig({"provider": "codex"}, str(workspace)))
            self.assertEqual(config["providerProfileName"], "codex")
            self.assertEqual(config["openaiApiMode"], "codex")
            self.assertTrue(is_runtime_secret_ref(config["openaiToken"]))
            self.assertEqual(run(resolve_provider_config_secrets(config))["openaiToken"], "codex-access-token")
        finally:
            if prev_openai is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = prev_openai
            self.restore_home(root, previous)
