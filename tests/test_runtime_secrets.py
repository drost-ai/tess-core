from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import agent_core._facade as core
from agent_core.runtime.secret_provider import resolve_provider_config_secrets
from agent_core.runtime.secrets import (
    RuntimeSecretError,
    authorize_runtime_secret_write,
    is_runtime_secret_ref,
    migrate_legacy_mcp_server_secrets,
    resolve_runtime_secret,
    store_runtime_secret,
)


class RuntimeSecretStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-secrets-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.project.mkdir()
        self.previous_home = os.environ.get("AGENT_CORE_HOME")
        os.environ["AGENT_CORE_HOME"] = str(self.home)
        self.provider_env_keys = (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "OPENAI_API_KEY",
            "TESS_OPENAI_CODEX_AUTH_PATH",
            "TESS_PROVIDER",
        )
        self.previous_provider_env = {key: os.environ.get(key) for key in self.provider_env_keys}
        for key in self.provider_env_keys:
            os.environ.pop(key, None)
        os.environ["TESS_OPENAI_CODEX_AUTH_PATH"] = str(self.home / "missing-codex-auth.json")
        os.environ["TESS_PROVIDER"] = "anthropic"
        core.configure_runtime_home(str(self.home))

    async def asyncTearDown(self) -> None:
        core.reset_configured_runtime_home()
        if self.previous_home is None:
            os.environ.pop("AGENT_CORE_HOME", None)
        else:
            os.environ["AGENT_CORE_HOME"] = self.previous_home
        for key, value in self.previous_provider_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    async def store_string(self, purpose: str, key: str, value: str) -> dict:
        material = bytearray(value.encode())
        try:
            return await store_runtime_secret(
                purpose=purpose,
                idempotencyKey=key,
                secret=material,
                authorization=authorize_runtime_secret_write(purpose=purpose, idempotencyKey=key),
            )
        finally:
            material[:] = b"\0" * len(material)

    async def test_authorized_writes_are_opaque_deterministic_and_private(self) -> None:
        literal = "high-entropy-secret-9918"
        refs = await asyncio.gather(
            *(self.store_string("mcp_header", "server:authorization", literal) for _ in range(12))
        )
        self.assertTrue(all(ref == refs[0] for ref in refs))
        self.assertTrue(is_runtime_secret_ref(refs[0]))
        self.assertNotIn(literal, json.dumps(refs[0]))

        private_root = self.home / "private-secrets"
        records_dir = private_root / "records"
        self.assertEqual(private_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(records_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((private_root / "integrity.key").stat().st_mode & 0o777, 0o600)
        files = list(records_dir.iterdir())
        self.assertEqual([path.name for path in files], [f"{refs[0]['id']}.json"])
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        raw = files[0].read_text()
        self.assertNotIn(literal, raw)

        resolved = await resolve_runtime_secret(refs[0], "mcp_header")
        try:
            self.assertEqual(bytes(resolved).decode(), literal)
        finally:
            resolved[:] = b"\0" * len(resolved)

    async def test_authority_idempotency_and_purpose_confusion_fail_closed(self) -> None:
        secret = bytearray(b"never-show-this-value")
        with self.assertRaises(RuntimeSecretError) as raised:
            await store_runtime_secret(
                purpose="mcp_header",
                idempotencyKey="fake",
                secret=secret,
                authorization={
                    "kind": "runtime_secret_write_authorization",
                    "purpose": "mcp_header",
                    "idempotencyKey": "fake",
                    "expiresAt": 2**63,
                },
            )
        self.assertEqual(raised.exception.code, "secret_store_unauthorized")
        self.assertNotIn("never-show", str(raised.exception))

        ref = await self.store_string("mcp_header", "same-key", "first")
        with self.assertRaises(RuntimeSecretError) as reused:
            await self.store_string("mcp_header", "same-key", "different")
        self.assertEqual(reused.exception.code, "secret_store_unauthorized")
        with self.assertRaises(RuntimeSecretError) as confused:
            await resolve_runtime_secret({**ref, "purpose": "mcp_env"}, "mcp_env")
        self.assertEqual(confused.exception.code, "secret_store_invalid")

    async def test_missing_symlinked_and_widened_state_is_rejected(self) -> None:
        ref = await self.store_string("mcp_env", "server:token", "contained")
        record = self.home / "private-secrets" / "records" / f"{ref['id']}.json"
        record.chmod(0o644)
        with self.assertRaisesRegex(RuntimeSecretError, "permissions are not owner-only"):
            await resolve_runtime_secret(ref, "mcp_env")

        record.chmod(0o600)
        moved = record.with_suffix(".real")
        moved.write_bytes(record.read_bytes())
        moved.chmod(0o600)
        record.unlink()
        record.symlink_to(moved)
        with self.assertRaisesRegex(RuntimeSecretError, "not a regular file"):
            await resolve_runtime_secret(ref, "mcp_env")

        record.unlink()
        with self.assertRaises(FileNotFoundError):
            await resolve_runtime_secret(ref, "mcp_env")

    async def test_user_mcp_migrates_once_and_project_literals_are_rejected(self) -> None:
        literal = "legacy-bearer-777"
        records = [
            {
                "name": "legacy-http",
                "transport": "http",
                "commandOrUrl": "https://example.test/mcp",
                "headers": {"Authorization": literal},
                "oauth": {"clientSecret": literal},
            },
            {
                "name": "legacy-stdio",
                "transport": "stdio",
                "commandOrUrl": "/bin/echo",
                "env": {"PUBLIC_VALUE": "visible", "SERVICE_TOKEN": literal},
            },
        ]
        await core.save_mcp_servers("user", str(self.project), records)
        first = await core.resolve_config({"settingSources": "user"}, str(self.project))
        by_name = {record["name"]: record for record in first["mcpConfig"]}
        self.assertTrue(is_runtime_secret_ref(by_name["legacy-http"]["headers"]["Authorization"]))
        self.assertTrue(is_runtime_secret_ref(by_name["legacy-http"]["oauth"]["clientSecret"]))
        self.assertTrue(is_runtime_secret_ref(by_name["legacy-stdio"]["env"]["SERVICE_TOKEN"]))
        self.assertEqual(by_name["legacy-stdio"]["env"]["PUBLIC_VALUE"], "visible")
        self.assertNotIn(literal, json.dumps(first))
        files = sorted((self.home / "private-secrets" / "records").iterdir())
        self.assertEqual(len(files), 3)

        second = await core.resolve_config({"settingSources": "user"}, str(self.project))
        self.assertEqual(second["mcpConfig"], first["mcpConfig"])
        self.assertEqual(sorted(path.name for path in files), sorted(path.name for path in (self.home / "private-secrets" / "records").iterdir()))
        fragments = sorted((self.home / "settings" / "mcp").glob("*.json"))
        self.assertEqual([path.name for path in fragments], ["legacy-http.json", "legacy-stdio.json"])
        for path in fragments:
            self.assertNotIn(literal, path.read_text())

        await core.save_mcp_servers("project", str(self.project), records[:1])
        with self.assertRaisesRegex(RuntimeSecretError, "requires a runtime secret reference"):
            await core.resolve_config({"settingSources": "project"}, str(self.project))

    async def test_legacy_migration_is_restart_idempotent(self) -> None:
        records = [{
            "name": "legacy",
            "transport": "http",
            "commandOrUrl": "https://example.test/mcp",
            "headers": {"Authorization": "restart-safe"},
        }]
        first = await migrate_legacy_mcp_server_secrets(records)
        second = await migrate_legacy_mcp_server_secrets(records)
        self.assertEqual(second["records"], first["records"])
        self.assertEqual(len(list((self.home / "private-secrets" / "records").iterdir())), 1)

    async def test_provider_auth_is_persisted_as_refs_and_resolved_just_in_time(self) -> None:
        api_key = "provider-api-key-opaque-481"
        auth_token = "provider-oauth-opaque-927"
        await core.save_auth(
            {
                "anthropicApiKey": api_key,
                "apiKey": api_key,
                "anthropicAuthToken": auth_token,
                "authToken": auth_token,
            }
        )
        persisted = (self.home / "auth.json").read_text()
        self.assertNotIn(api_key, persisted)
        self.assertNotIn(auth_token, persisted)
        auth = await core.load_auth()
        self.assertTrue(is_runtime_secret_ref(auth["anthropicApiKey"]))
        self.assertEqual(auth["anthropicApiKey"], auth["apiKey"])
        self.assertTrue(is_runtime_secret_ref(auth["anthropicAuthToken"]))
        self.assertEqual(auth["anthropicAuthToken"], auth["authToken"])
        self.assertEqual(len(list((self.home / "private-secrets" / "records").iterdir())), 2)

        config = await core.resolve_config({"provider": "anthropic-console"}, str(self.project))
        self.assertTrue(is_runtime_secret_ref(config["apiKey"]))
        self.assertNotIn(api_key, json.dumps(config))
        self.assertNotIn(auth_token, json.dumps(config))
        resolved = await resolve_provider_config_secrets(config)
        self.assertEqual(resolved["apiKey"], api_key)
        self.assertIsNone(resolved["authToken"])

    async def test_empty_provider_environment_creates_no_secret_record(self) -> None:
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = ""
        try:
            config = await core.resolve_config(
                {"provider": "openai", "openaiApiMode": "chat_completions"},
                str(self.project),
            )
            self.assertIsNone(config["openaiToken"])
            records = self.home / "private-secrets" / "records"
            self.assertEqual(list(records.iterdir()) if records.exists() else [], [])
        finally:
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
