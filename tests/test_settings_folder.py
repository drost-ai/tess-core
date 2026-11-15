from __future__ import annotations

from tests.support.tess_core_case import *


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SettingsFolderTests(TessCoreTestCase):
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
            settings = run(core.load_settings("user", str(workspace)))
            self.assertEqual(settings["provider"], "fake")
            self.assertEqual(settings["model"], "test-model")
            self.assertEqual(settings["durableMemory"], {"enabled": False})
            self.assertEqual(settings["providerProfiles"]["fake"]["kind"], "openai")
            self.assertEqual(settings["env"], {"FAKE_KEY": "abc123", "QUOTED": "v 1"})
        finally:
            self.restore_home(root, previous)

    def test_config_fragments_merge_in_filename_order_with_deep_merge(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config.yaml", "permissionMode: default\nknobs:\n  subagents:\n    max_live: 4\n")
            _write(home / "settings" / "config" / "20-comms.yaml", "knobs:\n  comms:\n    reply_poll_ms: 250\n")
            _write(home / "settings" / "config" / "10-daemon.yaml", "knobs:\n  daemon:\n    heartbeat_ms: 5000\n")
            _write(home / "settings" / "config" / "empty.yaml", "# only a comment\n")
            _write(home / "settings" / "config" / ".hidden.yaml", "permissionMode: plan\n")
            _write(home / "settings" / "config" / "notes.txt", "not: yaml\n")
            settings = run(core.load_settings("user", str(workspace)))
            self.assertEqual(settings["permissionMode"], "default")
            self.assertEqual(settings["knobs"], {
                "subagents": {"max_live": 4},
                "daemon": {"heartbeat_ms": 5000},
                "comms": {"reply_poll_ms": 250},
            })
        finally:
            self.restore_home(root, previous)

    def test_same_setting_in_two_files_is_fatal_naming_both(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config.yaml", "knobs:\n  daemon:\n    heartbeat_ms: 3000\n")
            _write(home / "settings" / "config" / "daemon.yaml", "knobs:\n  daemon:\n    heartbeat_ms: 5000\n")
            with self.assertRaises(ValueError) as base_conflict:
                run(core.load_settings("user", str(workspace)))
            self.assertIn("knobs.daemon.heartbeat_ms", str(base_conflict.exception))
            self.assertIn("config.yaml", str(base_conflict.exception))
            self.assertIn("config/daemon.yaml", str(base_conflict.exception))

            (home / "settings" / "config.yaml").unlink()
            _write(home / "settings" / "config" / "extra.yaml", "knobs:\n  daemon:\n    heartbeat_ms: 4000\n")
            with self.assertRaises(ValueError) as fragment_conflict:
                run(core.load_settings("user", str(workspace)))
            self.assertIn("config/daemon.yaml", str(fragment_conflict.exception))
            self.assertIn("config/extra.yaml", str(fragment_conflict.exception))

            # Section-vs-value mismatch is a conflict too, not a merge.
            (home / "settings" / "config" / "extra.yaml").unlink()
            _write(home / "settings" / "config" / "zz-flat.yaml", "knobs: 5\n")
            with self.assertRaises(ValueError) as shape_conflict:
                run(core.load_settings("user", str(workspace)))
            self.assertIn("knobs", str(shape_conflict.exception))
            self.assertIn("config/zz-flat.yaml", str(shape_conflict.exception))
        finally:
            self.restore_home(root, previous)

    def test_fragment_must_be_a_mapping_and_providers_yaml_still_wins(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config" / "bad.yaml", "- just\n- a\n- list\n")
            with self.assertRaises(ValueError) as bad:
                run(core.load_settings("user", str(workspace)))
            self.assertIn("mapping", str(bad.exception))

            (home / "settings" / "config" / "bad.yaml").unlink()
            _write(home / "settings" / "config" / "selection.yaml", "provider: from-fragment\nmodel: fragment-model\n")
            _write(home / "settings" / "providers.yaml", "provider: from-providers\n")
            settings = run(core.load_settings("user", str(workspace)))
            self.assertEqual(settings["provider"], "from-providers")  # providers.yaml wins for its keys
            self.assertEqual(settings["model"], "fragment-model")
        finally:
            self.restore_home(root, previous)

    def test_fragment_knobs_reach_knob_values_via_resolve_config(self):
        from tess_core.config._knobs import knob_value

        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config" / "subagents.yaml", "knobs:\n  subagents:\n    max_live: 3\n")
            run(core.resolve_config({"provider": "openai"}, str(workspace)))
            self.assertEqual(knob_value("subagents.max_live"), 3)
        finally:
            self.restore_home(root, previous)

    def test_legacy_settings_json_still_reads_without_folder(self):
        root, home, workspace, previous = self.temp_context()
        try:
            (home / "settings.json").write_text(json.dumps({"provider": "legacy"}), encoding="utf-8")
            settings = run(core.load_settings("user", str(workspace)))
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
            settings = run(core.load_settings("user", str(workspace)))
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
            settings = run(core.load_settings("user", str(workspace)))
            self.assertEqual(settings["provider"], "from-providers")
        finally:
            self.restore_home(root, previous)

    def test_malformed_env_line_raises_with_location(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / ".env", "GOOD=1\nnot a pair\n")
            with self.assertRaises(ValueError) as ctx:
                run(core.load_settings("user", str(workspace)))
            self.assertIn(".env:2", str(ctx.exception))
        finally:
            self.restore_home(root, previous)

    def test_non_mapping_yaml_document_raises(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config.yaml", "- just\n- a list\n")
            with self.assertRaises(ValueError) as ctx:
                run(core.load_settings("user", str(workspace)))
            self.assertIn("must be a mapping", str(ctx.exception))
        finally:
            self.restore_home(root, previous)

    def test_yaml_outside_subset_raises_with_line(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config.yaml", "a: &anchor 1\n")
            with self.assertRaises(ValueError) as ctx:
                run(core.load_settings("user", str(workspace)))
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
            config = run(core.resolve_config({}, str(workspace)))
            self.assertEqual(config.get("providerProfileName"), "folder-openai")
            self.assertEqual(config.get("model"), "folder-model")
        finally:
            self.restore_home(root, previous)

    def test_prompt_files_flow_through_resolve_config_into_session_stamping(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "system_prompt.md", "You are a folder-configured agent.\n\nBe terse.\n")
            _write(home / "settings" / "system_prompt_append.md", "Always answer in haiku.  \n\n")
            config = run(core.resolve_config({}, str(workspace)))
            self.assertEqual(config["systemPrompt"], "You are a folder-configured agent.\n\nBe terse.")
            self.assertEqual(config["appendSystemPrompt"], "Always answer in haiku.")

            session = run(core.resolve_session({}, config))
            self.assertEqual(
                session["systemPrompt"],
                "You are a folder-configured agent.\n\nBe terse.\n\nAlways answer in haiku.",
            )
        finally:
            self.restore_home(root, previous)

    def test_options_beat_prompt_files_and_append_composes_onto_builtin(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "system_prompt_append.md", "Cite sources.\n")
            config = run(core.resolve_config({}, str(workspace)))
            self.assertEqual(config["systemPrompt"], core.build_default_system_prompt())
            session = run(core.resolve_session({}, config))
            self.assertEqual(session["systemPrompt"], f"{core.build_default_system_prompt()}\n\nCite sources.")

            _write(home / "settings" / "system_prompt.md", "file prompt\n")
            overridden = run(
                core.resolve_config({"systemPrompt": "option prompt", "appendSystemPrompt": "option append"}, str(workspace))
            )
            self.assertEqual(overridden["systemPrompt"], "option prompt")
            self.assertEqual(overridden["appendSystemPrompt"], "option append")
        finally:
            self.restore_home(root, previous)

    def test_whitespace_only_prompt_files_are_skipped(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "system_prompt.md", "   \n\n\t\n")
            _write(home / "settings" / "system_prompt_append.md", "")
            settings = run(core.load_settings("user", str(workspace)))
            self.assertNotIn("systemPromptFile", settings)
            self.assertNotIn("systemPromptAppendFile", settings)
            config = run(core.resolve_config({}, str(workspace)))
            self.assertEqual(config["systemPrompt"], core.build_default_system_prompt())
            self.assertIsNone(config["appendSystemPrompt"])
        finally:
            self.restore_home(root, previous)

    def test_resolve_config_reads_permission_mode_from_settings_folder(self):
        # Headless embedders grant full permissions durably via
        # settings/config.yaml; an explicit option still wins.
        root, home, workspace, previous = self.temp_context()
        try:
            _write(home / "settings" / "config.yaml", "permissionMode: bypassPermissions\n")
            config = run(core.resolve_config({"provider": "openai"}, str(workspace)))
            self.assertEqual(config["permissionMode"], "bypassPermissions")
            overridden = run(core.resolve_config({"provider": "openai", "permissionMode": "default"}, str(workspace)))
            self.assertEqual(overridden["permissionMode"], "default")
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
