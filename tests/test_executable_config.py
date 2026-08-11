from __future__ import annotations

import json

from tests.support.agent_core_case import *


class ExecutableConfigTests(AgentCoreTestCase):
    def test_mcp_parser_normalizes_valid_legacy_records(self):
        stdio = core.parse_mcp_server_config(
            {
                "name": "local-tools",
                "transport": "stdio",
                "commandOrUrl": " node ",
                "args": ["server.py"],
                "env": {"MODE": "safe"},
            }
        )
        self.assertEqual(stdio["commandOrUrl"], "node")
        self.assertEqual(stdio["updatedAt"], core.LEGACY_UPDATED_AT)
        self.assertEqual(stdio["env"], {"MODE": "safe"})

        http = core.parse_mcp_server_config(
            {
                "name": "remote",
                "transport": "http",
                "commandOrUrl": "https://EXAMPLE.com:443/mcp",
                "headers": {"X-Mode": "safe"},
                "oauth": {
                    "scopes": ["tools:read"],
                    "redirect": {"mode": "loopback", "port": 43123},
                },
            }
        )
        self.assertEqual(http["commandOrUrl"], "https://example.com/mcp")
        self.assertEqual(http["oauth"]["redirect"]["port"], 43123)

    def test_mcp_parser_rejects_contradictions_and_mixed_invalid_lists(self):
        invalid = (
            {},
            {
                "name": "bad",
                "transport": "stdio",
                "commandOrUrl": "node",
                "headers": {"Authorization": "secret"},
            },
            {
                "name": "bad",
                "transport": "http",
                "commandOrUrl": "https://user:pass@example.com/mcp",
            },
            {
                "name": "bad-timeout",
                "transport": "stdio",
                "commandOrUrl": "node",
                "requestTimeoutMs": 0,
            },
        )
        for record in invalid:
            with self.subTest(record=record):
                with self.assertRaises(core.ExecutableConfigValidationError):
                    core.parse_mcp_server_config(record)

        with self.assertRaises(core.ExecutableConfigValidationError) as mixed:
            core.parse_mcp_server_config_list(
                [
                    {"name": "good", "transport": "stdio", "commandOrUrl": "node"},
                    {},
                ]
            )
        self.assertIn("[1].name", mixed.exception.field)
        with self.assertRaises(core.ExecutableConfigValidationError):
            core.parse_mcp_server_config_list(
                [
                    {"name": "same", "transport": "stdio", "commandOrUrl": "node"},
                    {"name": "SAME", "transport": "stdio", "commandOrUrl": "node"},
                ]
            )

    def test_plugin_and_installed_record_parsers_never_invent_identity(self):
        for manifest in ({}, {"id": "p", "version": "1", "entry": "../escape.py"}):
            with self.subTest(manifest=manifest):
                with self.assertRaises(core.ExecutableConfigValidationError):
                    core.parse_plugin_manifest_v1(manifest)
        with self.assertRaises(core.ExecutableConfigValidationError):
            core.parse_plugin_manifest_v1(
                {"id": "p", "version": "1", "commands": [{"name": "run"}]}
            )

        record = core.parse_installed_plugin_record(
            {
                "manifest": {"id": "installed", "version": "1"},
                "enabled": True,
                "scope": "project",
                "source": "./plugin",
            }
        )
        self.assertEqual(record["manifest"]["id"], "installed")
        self.assertEqual(record["installedAt"], core.LEGACY_UPDATED_AT)

    def test_resolve_config_uses_validator_without_echoing_secret_values(self):
        root, home, workspace, previous = self.temp_context()
        try:
            sentinel = "DO_NOT_ECHO_SENTINEL"
            invalid = home / "invalid.json"
            invalid.write_text(
                json.dumps(
                    {
                        "name": "broken",
                        "transport": "invalid",
                        "commandOrUrl": "node",
                        "headers": {"Authorization": sentinel},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(core.ExecutableConfigValidationError) as error:
                run(
                    core.resolve_config(
                        {"strictMcpConfig": True, "mcpConfig": [str(invalid)]},
                        str(workspace),
                    )
                )
            self.assertNotIn(sentinel, str(error.exception))
        finally:
            self.restore_home(root, previous)
