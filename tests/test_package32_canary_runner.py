from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path
from unittest import mock

from tests.support.agent_core_case import AgentCoreTestCase

import agent_core as core
from agent_core.providers.anthropic.payloads import buildAnthropicMessagesEnvelope
from agent_core.providers.errors import createProviderFailureRecord, createProviderRequestError
from agent_core_harness.package32_canary import main, runPackage32AnthropicCanary
from agent_core_harness.package32_canary_cli import runPackage32CanaryCli
from agent_core_harness.package32_canary_evidence import (
    Package32CanaryError,
    preparePrivateCanaryRuntime,
    reservePrivateEvidenceFile,
    safeFailureEvidence,
)


class Package32CanaryRunnerTests(AgentCoreTestCase):
    def test_provider_failure_evidence_is_closed_and_content_free(self):
        error = createProviderRequestError(
            createProviderFailureRecord(
                provider="anthropic",
                providerLabel="official",
                phase="token_count",
                category="auth",
                retryable=False,
                attempt=1,
                hadAssistantOutput=False,
                hadToolUse=False,
                rawMessage="sentinel-provider-body",
            )
        )
        evidence = safeFailureEvidence({"status": "running"}, error)
        self.assertEqual(evidence["failureReason"], "provider_token_count_auth")
        self.assertNotIn("sentinel", json.dumps(evidence))

    def test_cli_refuses_unauthorized_run_before_creating_paths(self):
        with tempfile.TemporaryDirectory() as root:
            runtime_home = Path(root, "runtime")
            evidence_file = Path(root, "evidence", "canary.json")
            result = main(
                [
                    "--model",
                    "claude-opus-4-8",
                    "--runtime-home",
                    str(runtime_home),
                    "--evidence-file",
                    str(evidence_file),
                    "--max-cost-usd",
                    "1.25",
                ]
            )
            self.assertEqual(result, 2)
            self.assertFalse(runtime_home.exists())
            self.assertFalse(evidence_file.exists())

    def test_cli_loads_anthropic_auth_token_from_explicit_credential_home(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            credential_home = base / "credentials"
            workspace = base / "workspace"
            credential_home.mkdir(mode=0o700)
            workspace.mkdir()

            async def seed_auth():
                async with core.scoped_runtime_home(str(credential_home)):
                    await core.ensure_base_layout(str(workspace))
                    await core.save_auth(
                        {
                            "provider": "claudeai",
                            "source": "test",
                            "authToken": "stale-auth-file-token",
                        }
                    )
                    await core.save_settings(
                        "user",
                        str(workspace),
                        {
                            "providerProfiles": {
                                "anthropic-claude-code-opus": {
                                    "kind": "anthropic",
                                    "defaultModel": "claude-opus-4-8",
                                    "auth": {
                                        "kind": "anthropic_auth_token",
                                        "envVar": "ANTHROPIC_AUTH_TOKEN",
                                    },
                                }
                            },
                            "env": {
                                "ANTHROPIC_AUTH_TOKEN": "sentinel-oauth-token",
                            },
                        },
                    )

            asyncio.run(seed_auth())
            received = {}

            def fake_canary(options):
                received.update(options)
                return {"status": "passed", "actualCostUsd": 0.0}

            with mock.patch.dict(
                os.environ,
                {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""},
            ):
                result = runPackage32CanaryCli(
                    fake_canary,
                    [
                        "--model",
                        "claude-opus-4-8",
                        "--runtime-home",
                        str(base / "runtime"),
                        "--credential-home",
                        str(credential_home),
                        "--provider-profile",
                        "anthropic-claude-code-opus",
                        "--evidence-file",
                        str(base / "evidence.json"),
                        "--max-cost-usd",
                        "1.25",
                        "--authorize-paid-provider-calls",
                    ],
                )
            self.assertEqual(result, 0)
            self.assertEqual(received["authToken"], "sentinel-oauth-token")
            self.assertIsNone(received["apiKey"])
            self.assertEqual(
                received["providerProfileName"],
                "anthropic-claude-code-opus",
            )

    def test_authorized_fake_trace_writes_private_content_free_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            runtime_home = base / "runtime"
            evidence_file = base / "evidence" / "canary.json"
            calls = []
            json_reasons = (
                "warmup_no_comparison",
                "no_divergence",
                "no_divergence",
                "no_divergence",
                "system_changed",
            )

            async def measure(_config, _request):
                return {"source": "anthropic_count", "inputTokens": 4_096}

            async def send(config, request, stream):
                envelope = buildAnthropicMessagesEnvelope(
                    request,
                    config,
                    {"stream": stream},
                )
                sequence = len(calls)
                calls.append({"stream": stream})
                local_index = sequence - 5 if stream else sequence
                creation = 1_500 if local_index == 0 or (not stream and local_index == 4) else 0
                read = 0 if creation else 1_200
                usage = {
                    "input_tokens": 20,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": creation,
                    "cache_creation_5m_input_tokens": creation if not stream else 0,
                    "cache_creation_1h_input_tokens": creation if stream else 0,
                    "cache_read_input_tokens": read,
                    "cache_creation_ttl_detail_complete": True,
                }
                observation = {
                    **envelope["promptCache"],
                    "responseId": f"msg_secret_{sequence}",
                    "providerDiagnosis": (
                        "not_requested" if stream else json_reasons[local_index]
                    ),
                }
                return {
                    "content": [{"type": "text", "text": "OK"}],
                    "model": "claude-opus-4-8",
                    "stopReason": "end_turn",
                    "usage": usage,
                    "promptCache": observation,
                }

            evidence = runPackage32AnthropicCanary(
                {
                    "authorized": True,
                    "model": "claude-opus-4-8",
                    "runtimeHome": str(runtime_home),
                    "evidenceFile": str(evidence_file),
                    "maxCostUsd": 10.0,
                    "apiKey": "sentinel-secret-key",
                    "_sendRequest": send,
                    "_measureTokens": measure,
                }
            )
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(len(calls), 8)
            self.assertEqual(evidence["requestCounts"], {"json5m": 5, "stream1h": 3})
            self.assertTrue(all(evidence["assertions"].values()))
            serialized = evidence_file.read_text(encoding="utf-8")
            self.assertNotIn("sentinel-secret-key", serialized)
            self.assertNotIn("msg_secret", serialized)
            self.assertNotIn("claude-opus-4-8", serialized)
            self.assertNotIn("cache-prefix", serialized)
            self.assertLess(len(serialized.encode("utf-8")), 128 * 1024)
            self.assertEqual(stat.S_IMODE(runtime_home.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(evidence_file.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(evidence_file.stat().st_mode), 0o600)

    def test_preflight_refuses_before_paths_or_provider_activity(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            runtime_home = base / "runtime"
            evidence_file = base / "evidence" / "canary.json"
            with self.assertRaises(Package32CanaryError) as raised:
                runPackage32AnthropicCanary(
                    {
                        "authorized": False,
                        "model": "claude-opus-4-8",
                        "runtimeHome": str(runtime_home),
                        "evidenceFile": str(evidence_file),
                        "maxCostUsd": 10.0,
                        "apiKey": "sentinel-secret-key",
                    }
                )
            self.assertEqual(raised.exception.code, "authorization_required")
            self.assertFalse(runtime_home.exists())
            self.assertFalse(evidence_file.exists())

    def test_cost_preflight_stops_before_token_count_or_messages(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            runtime_home = base / "runtime"
            evidence_file = base / "evidence" / "canary.json"
            activity = []

            async def measure(_config, _request):
                activity.append("count")
                return {"source": "anthropic_count", "inputTokens": 4_096}

            async def send(_config, _request, _stream):
                activity.append("message")
                raise AssertionError("provider must not run")

            with self.assertRaises(Package32CanaryError) as raised:
                runPackage32AnthropicCanary(
                    {
                        "authorized": True,
                        "model": "claude-opus-4-8",
                        "runtimeHome": str(runtime_home),
                        "evidenceFile": str(evidence_file),
                        "maxCostUsd": 0.000001,
                        "apiKey": "sentinel-secret-key",
                        "_sendRequest": send,
                        "_measureTokens": measure,
                    }
                )
            self.assertEqual(raised.exception.code, "cost_ceiling_exceeded")
            self.assertEqual(activity, [])
            failed = json.loads(evidence_file.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failureReason"], "cost_ceiling_exceeded")

    def test_existing_nonprivate_evidence_parent_is_not_chmodded(self):
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root, "shared")
            parent.mkdir(mode=0o755)
            parent.chmod(0o755)
            with self.assertRaises(Package32CanaryError) as raised:
                reservePrivateEvidenceFile(str(parent / "canary.json"))
            self.assertEqual(raised.exception.code, "private_path_unsafe")
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)
            with self.assertRaises(Package32CanaryError) as raised:
                preparePrivateCanaryRuntime(str(parent / "runtime"))
            self.assertEqual(raised.exception.code, "private_path_unsafe")
            self.assertFalse(Path(parent, "runtime").exists())
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)


if __name__ == "__main__":
    import unittest

    unittest.main()
