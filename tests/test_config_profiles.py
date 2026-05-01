from __future__ import annotations

from tests.support.agent_core_case import *


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
            self.assertEqual(config["openaiToken"], "deepinfra-test-key")
            self.assertEqual(config["model"], "zai-org/GLM-5")
        finally:
            if prev_key is None:
                os.environ.pop("DEEPINFRA_API_KEY", None)
            else:
                os.environ["DEEPINFRA_API_KEY"] = prev_key
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
                            },
                        },
                    }
                )
            )
            config = run(core.resolveConfig({"provider": "deepseek"}, str(workspace)))
            self.assertEqual(config["providerProfileName"], "deepseek")
            self.assertEqual(config["model"], "deepseek-v4-pro")
            self.assertEqual(config["openaiBaseUrl"], "https://api.deepseek.com")
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
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
            self.assertEqual(token_config["authToken"], "sk-ant-o-test-token")
            self.assertIsNone(token_config["apiKey"])

            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api-test-key"
            key_config = run(core.resolveConfig({"provider": "anthropic"}, str(workspace)))
            self.assertEqual(key_config["providerProfileName"], "anthropic-console")
            self.assertEqual(key_config["apiKey"], "sk-ant-api-test-key")
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
            self.assertEqual(token_config["authToken"], "sk-ant-o-stored-token")
            self.assertIsNone(token_config["apiKey"])

            (home / "auth.json").write_text(json.dumps({"provider": "console", "apiKey": "sk-ant-api-stored-key"}))
            key_config = run(core.resolveConfig({"provider": "anthropic"}, str(workspace)))
            self.assertEqual(key_config["providerProfileName"], "anthropic-console")
            self.assertEqual(key_config["apiKey"], "sk-ant-api-stored-key")
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
            self.assertEqual(config["openaiToken"], "codex-access-token")
        finally:
            if prev_openai is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = prev_openai
            self.restore_home(root, previous)
