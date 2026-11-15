from __future__ import annotations

from tests.support.tess_core_case import *


def _server(name: str, **overrides) -> dict:
    return {
        "name": name,
        "transport": "http",
        "commandOrUrl": f"https://example.test/{name}",
        "updatedAt": "now",
        **overrides,
    }


class McpSettingsFragmentTests(TessCoreTestCase):
    def test_user_scope_reads_fragment_directory(self):
        root, home, workspace, previous = self.temp_context()
        try:
            fragments = home / "settings" / "mcp"
            fragments.mkdir(parents=True)
            (fragments / "beta.json").write_text(json.dumps(_server("beta")))
            (fragments / "alpha.json").write_text(json.dumps([_server("alpha"), _server("alpha-extra")]))
            (fragments / "alpha.toml").write_text("ignored = true\n")
            (fragments / ".hidden.json").write_text(json.dumps(_server("hidden")))
            servers = run(core.load_mcp_servers("user", str(workspace)))
            self.assertEqual(
                [server["name"] for server in servers],
                ["alpha", "alpha-extra", "beta"],
            )
        finally:
            self.restore_home(root, previous)

    def test_duplicate_server_names_across_fragments_fail_loud(self):
        root, home, workspace, previous = self.temp_context()
        try:
            fragments = home / "settings" / "mcp"
            fragments.mkdir(parents=True)
            (fragments / "one.json").write_text(json.dumps(_server("shared")))
            (fragments / "two.json").write_text(json.dumps(_server("shared")))
            with self.assertRaisesRegex(ValueError, "exactly one file"):
                run(core.load_mcp_servers("user", str(workspace)))
        finally:
            self.restore_home(root, previous)

    def test_legacy_user_mcp_json_fails_loud(self):
        root, home, workspace, previous = self.temp_context()
        try:
            (home / "mcp.json").write_text(json.dumps([_server("legacy")]))
            with self.assertRaisesRegex(ValueError, "no longer read"):
                run(core.load_mcp_servers("user", str(workspace)))
            with self.assertRaisesRegex(ValueError, "no longer read"):
                run(core.save_mcp_servers("user", str(workspace), [_server("new")]))
        finally:
            self.restore_home(root, previous)

    def test_save_preserves_owner_files_and_removes_departed_servers(self):
        root, home, workspace, previous = self.temp_context()
        try:
            fragments = home / "settings" / "mcp"
            fragments.mkdir(parents=True)
            (fragments / "custom-name.json").write_text(json.dumps(_server("alpha")))
            (fragments / "beta.json").write_text(json.dumps(_server("beta")))
            run(
                core.save_mcp_servers(
                    "user",
                    str(workspace),
                    [_server("alpha", updatedAt="later"), _server("gamma")],
                )
            )
            names = sorted(path.name for path in fragments.glob("*.json"))
            self.assertEqual(names, ["custom-name.json", "gamma.json"])
            self.assertEqual(
                json.loads((fragments / "custom-name.json").read_text())["updatedAt"],
                "later",
            )
        finally:
            self.restore_home(root, previous)

    def test_mutate_round_trips_through_fragments(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.save_mcp_servers("user", str(workspace), [_server("origin")]))

            def add(servers):
                return [*servers, _server("added")]

            updated = run(core.mutate_mcp_servers("user", str(workspace), add))
            self.assertEqual([server["name"] for server in updated], ["origin", "added"])
            reloaded = run(core.load_mcp_servers("user", str(workspace)))
            self.assertEqual(
                sorted(server["name"] for server in reloaded), ["added", "origin"]
            )
        finally:
            self.restore_home(root, previous)

    def test_malformed_fragment_and_non_record_entries_fail_loud(self):
        root, home, workspace, previous = self.temp_context()
        try:
            fragments = home / "settings" / "mcp"
            fragments.mkdir(parents=True)
            (fragments / "broken.json").write_text("{not json")
            with self.assertRaises(Exception):
                run(core.load_mcp_servers("user", str(workspace)))
            (fragments / "broken.json").write_text(json.dumps(["not-a-record"]))
            with self.assertRaisesRegex(ValueError, "server record object"):
                run(core.load_mcp_servers("user", str(workspace)))
        finally:
            self.restore_home(root, previous)

    def test_project_scope_keeps_state_dir_mcp_json(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.save_mcp_servers("project", str(workspace), [_server("proj")]))
            servers = run(core.load_mcp_servers("project", str(workspace)))
            self.assertEqual([server["name"] for server in servers], ["proj"])
            self.assertFalse((home / "settings" / "mcp").exists())
        finally:
            self.restore_home(root, previous)
