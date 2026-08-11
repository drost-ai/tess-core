from __future__ import annotations

import json
import os
import textwrap

from tests.support.agent_core_case import *
import agent_core.authority.executable_closure as closure_module


def _stdio_server(name: str) -> dict[str, Any]:
    return {"name": name, "transport": "stdio", "commandOrUrl": "/bin/echo", "args": [name]}


class ExecutableSourceTrustTests(AgentCoreTestCase):
    def tearDown(self):
        getattr(closure_module, "_set_executable_closure_test_hook_for_tests")(None)

    def test_setting_sources_are_strict_ordered_and_deduplicated(self):
        self.assertEqual(core.parse_setting_sources(None), ["user", "project", "local"])
        self.assertEqual(core.parse_setting_sources("local,user,local"), ["local", "user"])
        with self.assertRaisesRegex(ValueError, "Unknown setting source"):
            core.parse_setting_sources("user,unknown")
        with self.assertRaisesRegex(ValueError, "at least one"):
            core.parse_setting_sources("")

    def test_excluded_project_sources_cannot_trigger_mcp_or_plugin_execution(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.save_mcp_servers("project", str(workspace), [_stdio_server("project")]))
            marker = workspace / "plugin-ran.txt"
            plugin = workspace / "project-plugin"
            plugin.mkdir()
            manifest = {"id": "project-plugin", "version": "1.0.0", "entry": "entry.py"}
            (plugin / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
            (plugin / "entry.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nplugin = {{}}\n",
                encoding="utf-8",
            )
            run(
                core.save_plugins(
                    "project",
                    str(workspace),
                    [{"manifest": manifest, "enabled": True, "source": str(plugin)}],
                )
            )

            config = run(
                core.resolve_config(
                    {"runtimeHome": str(home), "settingSources": "user"},
                    str(workspace),
                )
            )
            self.assertEqual(config["mcpConfig"], [])
            loaded = run(core.load_plugin_runtime(config))
            self.assertEqual(loaded["plugins"], [])
            self.assertFalse(marker.exists())
        finally:
            self.restore_home(root, previous)
    def test_selected_project_mcp_requires_external_trust_and_invalidates_on_change(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.save_mcp_servers("project", str(workspace), [_stdio_server("approved")]))
            with self.assertRaises(core.ExecutableSourceTrustError):
                run(core.resolve_config({"settingSources": "project"}, str(workspace)))
            before = run(core.inspect_mcp_source_trust(cwd=str(workspace), scope="project"))
            self.assertFalse(before["approved"])
            run(core.approve_mcp_source(cwd=str(workspace), scope="project"))
            config = run(core.resolve_config({"settingSources": "project"}, str(workspace)))
            self.assertEqual([item["name"] for item in config["mcpConfig"]], ["approved"])
            self.assertEqual(config["mcpConfig"][0]["scope"], "project")

            run(core.save_mcp_servers("project", str(workspace), [_stdio_server("changed")]))
            with self.assertRaises(core.ExecutableSourceTrustError):
                run(core.resolve_config({"settingSources": "project"}, str(workspace)))
        finally:
            self.restore_home(root, previous)

    def test_strict_mcp_admits_only_explicit_records_and_excludes_plugin_servers(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.save_mcp_servers("user", str(workspace), [_stdio_server("ambient-user")]))
            run(core.save_mcp_servers("project", str(workspace), [_stdio_server("ambient-project")]))
            explicit = home / "explicit.json"
            explicit.write_text(json.dumps(_stdio_server("explicit")), encoding="utf-8")
            config = run(
                core.resolve_config(
                    {"strictMcpConfig": True, "mcpConfig": [str(explicit)]},
                    str(workspace),
                )
            )
            self.assertEqual([item["name"] for item in config["mcpConfig"]], ["explicit"])
            self.assertEqual(config["mcpConfigPolicy"], {"mode": "explicit_only"})
            self.assertEqual(
                [item["name"] for item in core.select_mcp_bootstrap_servers(config["mcpConfigPolicy"], config["mcpConfig"], [_stdio_server("plugin")])],
                ["explicit"],
            )
        finally:
            self.restore_home(root, previous)

    def test_project_plugin_executes_snapshot_not_post_snapshot_mutation(self):
        root, home, workspace, previous = self.temp_context()
        try:
            plugin = workspace / "plugin"
            marker = workspace / "marker.txt"
            plugin.mkdir()
            manifest = {"id": "snapshot-plugin", "version": "1.0.0", "entry": "entry.py"}
            (plugin / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

            def source(value: str) -> str:
                return textwrap.dedent(
                    f"""
                    from pathlib import Path
                    Path({str(marker)!r}).write_text({value!r}, encoding="utf-8")
                    plugin = {{}}
                    """
                )

            (plugin / "entry.py").write_text(source("approved"), encoding="utf-8")
            run(
                core.save_plugins(
                    "project",
                    str(workspace),
                    [{"manifest": manifest, "enabled": True, "source": str(plugin)}],
                )
            )
            config = run(core.resolve_config({"settingSources": "project"}, str(workspace)))
            with self.assertRaises(core.ExecutableSourceTrustError):
                run(core.load_plugin_runtime(config))
            run(core.approve_plugin_source(cwd=str(workspace), scope="project"))

            async def mutate_after_snapshot(phase: str):
                if phase == "after_snapshot":
                    (plugin / "entry.py").write_text(source("mutated"), encoding="utf-8")

            getattr(closure_module, "_set_executable_closure_test_hook_for_tests")(mutate_after_snapshot)
            loaded = run(core.load_plugin_runtime(config))
            self.assertEqual(marker.read_text(encoding="utf-8"), "approved")
            self.assertIn(os.path.join("trust", "snapshots"), loaded["plugins"][0]["source"])
        finally:
            self.restore_home(root, previous)

    def test_workspace_legacy_approval_hints_grant_no_authority(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(
                core.save_settings(
                    "local",
                    str(workspace),
                    {
                        "permissions": {"allow": ["Bash(git status:*)"]},
                        "hooks": {
                            "PreToolUse": [
                                {"matcher": "Bash", "command": "exit 0", "blocking": True}
                            ]
                        },
                    },
                )
            )
            legacy = workspace / ".tess" / "local"
            legacy.mkdir(parents=True, exist_ok=True)
            (legacy / "approved-hooks.json").write_text("{}", encoding="utf-8")
            (legacy / "approved-permissions.json").write_text("{}", encoding="utf-8")

            rules = run(core.load_permission_rules(cwd=str(workspace), sources=["local"]))
            hooks = run(core.load_hooks(cwd=str(workspace), sources=["local"]))
            self.assertFalse(next(rule for rule in rules if rule["effect"] == "allow")["approved"])
            self.assertFalse(hooks[0]["approved"])

            run(core.approve_permission_rules(str(workspace), "local"))
            run(core.approve_hooks(str(workspace), "local"))
            rules = run(core.load_permission_rules(cwd=str(workspace), sources=["local"]))
            hooks = run(core.load_hooks(cwd=str(workspace), sources=["local"]))
            self.assertTrue(next(rule for rule in rules if rule["effect"] == "allow")["approved"])
            self.assertTrue(hooks[0]["approved"])
            status = run(core.get_repository_trust_status(str(workspace)))
            self.assertTrue(status["recordPath"].startswith(str(home)))
            self.assertEqual(os.stat(status["recordPath"]).st_mode & 0o777, 0o600)
        finally:
            self.restore_home(root, previous)
