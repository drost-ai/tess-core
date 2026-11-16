from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tess_core import SessionOptions, start_runtime
from tess_core.authority.secret_files import looks_like_secret_file
from tess_core.authority.shell_command_policy import classify_read_only_shell_command
from tess_core.tools.shell_env import credential_variables, shell_environment

REGISTRY = """schema_version: 1
providerProfiles:
  mine:
    kind: openai
    apiMode: chat_completions
    baseUrl: https://provider.invalid/v1
    defaultModel: m
    auth:
      kind: custom_bearer
      envVar: MY_PROVIDER_KEY
"""


def _text(text: str) -> dict:
    return {"model": "scripted", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": text}]}


def _tool(name: str, tool_input: dict) -> dict:
    return {"model": "scripted", "stopReason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "tool_use", "id": "tu_1", "name": name, "input": tool_input}]}


class CredentialProtectionTests(unittest.TestCase):
    """What the model can reach of the host's credentials: nothing from the
    environment in any mode, and no credentials file without approval."""

    def test_credential_file_names(self) -> None:
        for path in (".env", ".env.local", "config/prod.env", "/home/u/.ssh/config", "~/.codex/auth.json", "server.pem", "id_ed25519", ".netrc"):
            self.assertTrue(looks_like_secret_file(path), path)
        for path in (".env.example", ".env.sample", "environment.py", ".envrc", "README.md", "src/keyboard.py", "docs/auth.json"):
            self.assertFalse(looks_like_secret_file(path), path)

    def test_a_shell_command_that_names_a_credentials_file_is_not_read_only(self) -> None:
        for command in ("cat .env", "cat ../.env", "cat ~/.codex/auth.json", "rg SECRET .env", "wc -l .env.local"):
            self.assertFalse(classify_read_only_shell_command(command)["safe"], command)
        for command in ("cat main.py", "cat .env.example", "ls -la", "rg TODO ."):
            self.assertTrue(classify_read_only_shell_command(command)["safe"], command)

    def test_shell_environment_drops_provider_credentials(self) -> None:
        environment = {"PATH": "/bin", "OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b", "MY_PROVIDER_KEY": "c", "FROM_HOST": "d", "GITHUB_TOKEN": "kept", "TESS_CORE_CREDENTIAL_ENV": "FROM_HOST"}
        config = {"providerProfiles": {"mine": {"auth": {"kind": "custom_bearer", "envVar": "MY_PROVIDER_KEY"}}}}
        self.assertEqual(set(shell_environment(config, environment)), {"PATH", "GITHUB_TOKEN", "TESS_CORE_CREDENTIAL_ENV"})
        self.assertIn("MY_PROVIDER_KEY", credential_variables(config, environment))

    def _run(self, mode: str, calls: list[tuple[str, dict]], listener: object | None = None) -> list[dict]:
        async def scenario(root: Path) -> list[dict]:
            (root / "work").mkdir()
            (root / "work" / ".env").write_text("TOKEN=file-secret-value\n", encoding="utf-8")
            (root / "providers.yaml").write_text(REGISTRY, encoding="utf-8")
            runtime = await start_runtime(
                root / "home", cwd=root / "work", daemon_enabled=False, provider_registry=root / "providers.yaml", listener=listener,
                session_options=lambda session_id, reason: SessionOptions(provider="mine", permission_mode=mode, extra={"settingSources": ""}),
            )
            results = []
            try:
                session = await runtime.sessions.open()
                for name, tool_input in calls:
                    requests: list[dict] = []
                    replies = [_tool(name, {key: value.replace("<work>", str(root / "work")) for key, value in tool_input.items()}), _text("done")]

                    async def send(request: dict) -> dict:
                        requests.append(request)
                        return replies[min(len(requests) - 1, 1)]

                    await session.run_turn("go", provider_override={"send": send})
                    results.append(requests[1]["messages"][-1]["content"][0])
            finally:
                await runtime.stop()
            return results

        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"MY_PROVIDER_KEY": "env-secret-value", "OPENAI_API_KEY": "env-secret-value"}):
            return asyncio.run(scenario(Path(folder).resolve()))

    def test_default_mode_refuses_every_route_to_a_credentials_file(self) -> None:
        calls = [("Read", {"file_path": "<work>/.env"}), ("Grep", {"pattern": "TOKEN", "path": "<work>/.env", "output_mode": "content"}), ("Bash", {"command": "cat .env"}), ("Bash", {"command": "env"})]
        for block in self._run("default", calls):
            self.assertTrue(block.get("is_error"), block)
            self.assertNotIn("secret-value", json.dumps(block))

    def test_a_host_is_asked_before_a_credentials_file_is_read(self) -> None:
        class Host:
            def __init__(self) -> None:
                self.asked: list[tuple[str, list[str]]] = []

            async def ask_question(self, session_id, question, options, detail=None):
                self.asked.append((detail["category"], options))
                return options[-1]

        host = Host()
        (block,) = self._run("default", [("Read", {"file_path": "<work>/.env"})], listener=host)
        self.assertTrue(block.get("is_error"), block)
        self.assertEqual(host.asked, [("secret", ["Approve once", "Deny"])])

    def test_bypass_mode_still_keeps_provider_keys_out_of_the_shell(self) -> None:
        results = self._run("bypassPermissions", [("Bash", {"command": "env"}), ("Bash", {"command": "echo $MY_PROVIDER_KEY$OPENAI_API_KEY"}), ("Read", {"file_path": "<work>/.env"})])
        for block in results[:2]:
            self.assertFalse(block.get("is_error"), block)
            self.assertNotIn("env-secret-value", json.dumps(block))
        # Bypass means bypass for files: the mode gives the agent the user's own reach.
        self.assertIn("file-secret-value", json.dumps(results[2]))


if __name__ == "__main__":
    unittest.main()
