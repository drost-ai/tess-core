from __future__ import annotations

from tests.support.agent_core_case import *


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SettingsFolderTests(AgentCoreTestCase):
    def test_folder_only_merges_env_providers_and_config(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config.yaml", "durableMemory:\n  enabled: false\nmodel: test-model\n")
            _write(
                home / "settings" / "providers.yaml",
                "provider: fake\n"
                "providerProfiles:\n"
                "  fake:\n"
                "    kind: openai\n"
                "    defaultModel: fake-model\n"
                "    baseUrl: https://example.invalid/v1\n",
            )
            _write(home / "settings" / ".env", "# creds\nexport FAKE_KEY=abc123\nQUOTED='v 1'\n")
            settings = run(core.loadSettings("user", str(workspace)))
            self.assertEqual(settings["provider"], "fake")
            self.assertEqual(settings["model"], "test-model")
            self.assertEqual(settings["durableMemory"], {"enabled": False})
            self.assertEqual(settings["providerProfiles"]["fake"]["kind"], "openai")
            self.assertEqual(settings["env"], {"FAKE_KEY": "abc123", "QUOTED": "v 1"})
        finally:
            self.restore_home(root, previous)

    def test_legacy_settings_json_still_reads_without_folder(self):
        root, home, workspace, previous = self.temp_context()
        try:
            (home / "settings.json").write_text(json.dumps({"provider": "legacy"}), encoding="utf-8")
            settings = run(core.loadSettings("user", str(workspace)))
            self.assertEqual(settings["provider"], "legacy")
        finally:
            self.restore_home(root, previous)

    def test_folder_overrides_legacy_per_key_and_deep_merges(self):
        root, home, workspace, previous = self.temp_context()
        try:
            (home / "settings.json").write_text(
                json.dumps(
                    {
                        "provider": "legacy",
                        "effort": "high",
                        "env": {"KEEP": "1", "OVERRIDE": "old"},
                        "providerProfiles": {
                            "legacy-profile": {"kind": "openai"},
                            "shared": {"kind": "openai", "defaultModel": "old"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            _write(
                home / "settings" / "providers.yaml",
                "provider: fresh\n"
                "providerProfiles:\n"
                "  shared:\n"
                "    kind: anthropic\n",
            )
            _write(home / "settings" / ".env", "OVERRIDE=new\n")
            settings = run(core.loadSettings("user", str(workspace)))
            self.assertEqual(settings["provider"], "fresh")
            self.assertEqual(settings["effort"], "high")
            self.assertEqual(settings["env"], {"KEEP": "1", "OVERRIDE": "new"})
            self.assertEqual(settings["providerProfiles"]["legacy-profile"], {"kind": "openai"})
            self.assertEqual(settings["providerProfiles"]["shared"], {"kind": "anthropic"})
        finally:
            self.restore_home(root, previous)

    def test_providers_yaml_wins_over_config_yaml(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config.yaml", "provider: from-config\n")
            _write(home / "settings" / "providers.yaml", "provider: from-providers\n")
            settings = run(core.loadSettings("user", str(workspace)))
            self.assertEqual(settings["provider"], "from-providers")
        finally:
            self.restore_home(root, previous)

    def test_malformed_env_line_raises_with_location(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / ".env", "GOOD=1\nnot a pair\n")
            with self.assertRaises(ValueError) as ctx:
                run(core.loadSettings("user", str(workspace)))
            self.assertIn(".env:2", str(ctx.exception))
        finally:
            self.restore_home(root, previous)

    def test_non_mapping_yaml_document_raises(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config.yaml", "- just\n- a list\n")
            with self.assertRaises(ValueError) as ctx:
                run(core.loadSettings("user", str(workspace)))
            self.assertIn("must be a mapping", str(ctx.exception))
        finally:
            self.restore_home(root, previous)

    def test_yaml_outside_subset_raises_with_line(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config.yaml", "a: &anchor 1\n")
            with self.assertRaises(ValueError) as ctx:
                run(core.loadSettings("user", str(workspace)))
            self.assertIn("anchors", str(ctx.exception))
        finally:
            self.restore_home(root, previous)

    def test_resolve_config_reads_provider_from_settings_folder(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(
                home / "settings" / "providers.yaml",
                "provider: folder-openai\n"
                "providerProfiles:\n"
                "  folder-openai:\n"
                "    kind: openai\n"
                "    defaultModel: folder-model\n"
                "    baseUrl: https://example.invalid/v1\n"
                "    auth:\n"
                "      kind: openai_api_key\n"
                "      envVar: FOLDER_TEST_KEY\n",
            )
            _write(home / "settings" / ".env", "FOLDER_TEST_KEY=sk-test\n")
            config = run(core.resolveConfig({}, str(workspace)))
            self.assertEqual(config.get("providerProfileName"), "folder-openai")
            self.assertEqual(config.get("model"), "folder-model")
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
