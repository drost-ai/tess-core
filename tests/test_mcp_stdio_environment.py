"""Subprocess options only: no child process, sockets, or real credentials."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from agent_core.authority.executable_config import parse_mcp_server_config
from agent_core.mcp.stdio_transport import StdioMcpClient


class McpStdioEnvironmentTests(unittest.TestCase):
    def _spawn_options(self, **fields):
        config = parse_mcp_server_config({"name": "isolated", "transport": "stdio", "commandOrUrl": "/fake/python", **fields})
        client = StdioMcpClient(config, "/fake/workspace")
        with mock.patch("agent_core.mcp.stdio_transport.subprocess.Popen", side_effect=OSError("synthetic spawn prevented")) as popen:
            with self.assertRaises(Exception):
                client._start_process()
        return popen.call_args.kwargs

    def test_explicit_environment_replaces_parent_and_cwd_is_honored(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/ambient", "AGENTIC_MEMORY_SETTINGS": "/ambient/private", "SYNTHETIC_OTHER": "must-not-inherit"}):
            kwargs = self._spawn_options(inheritEnv=False, env={"PATH": "/fake/bin", "ONLY": "synthetic"}, cwd="/fake/runtime")
        self.assertEqual(kwargs["env"], {"PATH": "/fake/bin", "ONLY": "synthetic"})
        self.assertEqual(kwargs["cwd"], "/fake/runtime")

    def test_default_retains_environment_and_workspace(self):
        with mock.patch.dict(os.environ, {"SYNTHETIC_PARENT": "keep"}):
            kwargs = self._spawn_options(env={"SYNTHETIC_CHILD": "add"})
        self.assertEqual(kwargs["env"]["SYNTHETIC_PARENT"], "keep")
        self.assertEqual(kwargs["env"]["SYNTHETIC_CHILD"], "add")
        self.assertEqual(kwargs["cwd"], "/fake/workspace")

    def test_no_environment_is_still_explicitly_empty(self):
        self.assertEqual(self._spawn_options(inheritEnv=False)["env"], {})

    def test_options_are_validated(self):
        base = {"name": "test", "transport": "stdio", "commandOrUrl": "/fake/python"}
        for fields in ({"inheritEnv": "false"}, {"cwd": 3}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                parse_mcp_server_config({**base, **fields})
        for fields in ({"inheritEnv": False}, {"cwd": "/fake/runtime"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                parse_mcp_server_config({**base, "transport": "http", "commandOrUrl": "https://example.invalid", **fields})
