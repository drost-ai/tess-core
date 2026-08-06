from __future__ import annotations

import hashlib
import json
import time
from unittest import mock

import agent_core as core
import agent_core.agent.context as context_module
from agent_core.context.request_preflight import (
    RECEIPT_MAX_BYTES,
    buildRequestPreflight,
    preflightEventPayload,
)
from tests.support.agent_core_case import AgentCoreTestCase, bind_test_runtime, run


def _receipt_fixture(
    *,
    trusted_skills: bool = True,
    history_pairs: int = 1,
    history_chars: int = 0,
) -> dict:
    system = "stable identity\nselected skill instructions"
    prompt_hash = hashlib.sha256(system.encode("utf-8")).hexdigest()
    image = "data:image/png;base64," + ("U0VDUkVU" * 512)
    history = []
    for index in range(history_pairs):
        suffix = "x" * history_chars
        history.extend(
            (
                {"role": "user", "content": f"prior user context {index}{suffix}"},
                {"role": "assistant", "content": f"prior answer {index}{suffix}"},
            )
        )
    current_message_index = len(history)
    request = {
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "system": system,
        "messages": [
            *history,
            {
                "role": "user",
                "metadata": {"host": {"turn_id": "turn-1"}},
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[Dynamic continuation context]\ncheckpoint overlay"
                            "\n[/Dynamic continuation context]\n\ncurrent request sentinel"
                        ),
                    },
                    {"type": "image", "source": {"type": "base64", "data": image}},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "Read",
                        "input": {"path": "sentinel-secret.txt"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "pending tool result sentinel",
                    }
                ],
            },
        ],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file sentinel",
                "input_schema": {"type": "object"},
            }
        ],
    }
    measurement = {
        "inputTokens": 4_223,
        "baseInputTokens": 4_000,
        "safetyMarginTokens": 223,
        "source": "codex_estimate",
        "measuredAt": "2026-07-26T00:00:00Z",
        "provider": "openai",
        "model": request["model"],
        "estimatedFixedInputTokens": 1_000,
        "estimatedMessageInputTokens": 2_000,
        "estimatedImageInputTokens": 1_000,
    }
    session = {
        "id": "session-1",
        "messages": request["messages"],
        "metadata": {
            "compactionState": {
                "latestCompactionGeneration": 3,
                "compactedThroughMessageIndex": 21,
            }
        },
    }
    return buildRequestPreflight(
        request=request,
        measurement=measurement,
        outputBudget={
            "configuredMaxOutputTokens": 128_000,
            "effectiveMaxOutputTokens": 64_000,
            "minUsefulOutputTokens": 256,
            "remainingOutputTokens": 64_000,
            "clamped": True,
        },
        contextBudget={
            "status": "ready",
            "reason": None,
            "compactionThresholdTokens": 200_000,
            "compactionTargetTokens": 150_000,
            "remainingHeadroomTokens": 195_777,
        },
        continuation={
            "systemContext": "checkpoint overlay",
            "preflightSegments": {
                "checkpointTokens": 120,
                "continuationOverlayTokens": 40,
                "retrievedMemoryTokens": 30,
                "reliefTokens": 10,
            },
            "relief": {"selectedMode": "none", "applied": False},
        },
        session=session,
        baseSystemPrompt=system,
        preflightHints={
            "systemPromptHash": prompt_hash if trusted_skills else "stale-hash",
            "selectedSkillChars": 20,
            "currentTurnId": "turn-1",
            "currentMessageIndex": current_message_index,
        },
        dynamicContextInMessages=True,
        compactionOutcome={"status": "skipped", "reason": "below_threshold"},
        providerProfileName="codex",
    )


def _production_sized_preflight_fixture() -> dict:
    system = "s" * 91_407
    messages = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"history-{index:03d}-" + ("m" * 420),
        }
        for index in range(248)
    ]
    messages[-1] = {
        "role": "user",
        "metadata": {"host": {"turn_id": "production-turn"}},
        "content": "current request " + ("c" * 420),
    }
    tools = [
        {
            "name": f"production_tool_{index:02d}",
            "description": "d" * 500,
            "input_schema": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "v" * 120},
                },
            },
        }
        for index in range(45)
    ]
    request = {
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "system": system,
        "messages": messages,
        "tools": tools,
    }
    target_chars = 268_953
    encoded_chars = len(
        json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    )
    assert encoded_chars < target_chars
    messages[-1]["content"] += "p" * (target_chars - encoded_chars)
    assert (
        len(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
        == target_chars
    )
    return {
        "request": request,
        "measurement": {
            "inputTokens": 67_239,
            "baseInputTokens": 67_000,
            "safetyMarginTokens": 239,
            "source": "production_fixture",
            "measuredAt": "2026-07-26T00:00:00Z",
            "provider": "openai",
            "model": request["model"],
            "estimatedFixedInputTokens": 28_000,
            "estimatedMessageInputTokens": 39_000,
            "estimatedImageInputTokens": 0,
        },
        "outputBudget": {
            "configuredMaxOutputTokens": 128_000,
            "effectiveMaxOutputTokens": 128_000,
            "minUsefulOutputTokens": 256,
            "remainingOutputTokens": 128_000,
            "clamped": False,
        },
        "contextBudget": {
            "status": "ready",
            "reason": None,
            "compactionThresholdTokens": 200_000,
            "compactionTargetTokens": 150_000,
            "remainingHeadroomTokens": 132_761,
        },
        "continuation": {
            "systemContext": "",
            "preflightSegments": {},
            "relief": {"selectedMode": "none", "applied": False},
        },
        "session": {
            "id": "production-session",
            "messages": messages,
            "metadata": {"compactionState": {}},
        },
        "baseSystemPrompt": system,
        "preflightHints": {
            "systemPromptHash": hashlib.sha256(system.encode()).hexdigest(),
            "selectedSkillChars": 12_000,
            "currentTurnId": "production-turn",
            "currentMessageIndex": 247,
        },
        "dynamicContextInMessages": True,
        "providerProfileName": "codex",
    }


class Package37RequestPreflightTests(AgentCoreTestCase):
    def test_receipt_is_bounded_content_free_and_exhaustively_accounted(self):
        receipt = _receipt_fixture()
        serialized = json.dumps(receipt, ensure_ascii=False)
        categories = receipt["categories"]

        self.assertLessEqual(len(serialized.encode("utf-8")), RECEIPT_MAX_BYTES)
        self.assertEqual(receipt["schemaVersion"], 1)
        self.assertTrue(receipt["provenance"]["selectedSkills"]["trusted"])
        self.assertTrue(receipt["provenance"]["currentTurn"]["trusted"])
        self.assertGreater(categories["selectedSkills"], 0)
        self.assertGreater(categories["toolDefinitions"], 0)
        self.assertGreater(categories["checkpoint"], 0)
        self.assertGreater(categories["continuationOverlays"], 0)
        self.assertGreater(categories["currentText"], 0)
        self.assertGreater(categories["currentMedia"], 0)
        self.assertGreater(categories["pendingToolResults"], 0)
        self.assertEqual(categories["unattributed"], 223)
        self.assertEqual(
            sum(value for key, value in categories.items() if key != "unattributed"),
            receipt["measurement"]["componentEstimateTokens"],
        )
        self.assertEqual(receipt["measurement"]["measurementDeltaTokens"], 223)
        self.assertNotIn("current request sentinel", serialized)
        self.assertNotIn("pending tool result sentinel", serialized)
        self.assertNotIn("sentinel-secret", serialized)
        self.assertNotIn("base64", serialized)

    def test_stale_skill_hint_cannot_reduce_system_identity(self):
        trusted = _receipt_fixture()
        stale = _receipt_fixture(trusted_skills=False)
        self.assertEqual(
            stale["provenance"]["selectedSkills"]["reason"],
            "prompt_hash_mismatch",
        )
        self.assertEqual(stale["categories"]["selectedSkills"], 0)
        self.assertGreaterEqual(
            stale["categories"]["systemIdentity"],
            trusted["categories"]["systemIdentity"],
        )

    def test_event_projection_is_the_same_content_free_receipt(self):
        receipt = _receipt_fixture()
        payload = preflightEventPayload("session-1", receipt)
        self.assertEqual(payload["categories"], receipt["categories"])
        self.assertEqual(
            payload["request_fingerprint"],
            receipt["requestFingerprint"],
        )
        payload["categories"]["currentText"] = 0
        self.assertGreater(receipt["categories"]["currentText"], 0)
        self.assertNotIn("sentinel", json.dumps(payload))

    def test_production_sized_projection_is_linear_and_below_gate(self):
        fixture = _production_sized_preflight_fixture()
        self.assertEqual(len(fixture["request"]["messages"]), 248)
        self.assertEqual(len(fixture["request"]["tools"]), 45)
        self.assertEqual(len(fixture["baseSystemPrompt"]), 91_407)
        samples = []
        for _ in range(30):
            started = time.perf_counter()
            buildRequestPreflight(**fixture)
            samples.append(time.perf_counter() - started)
        p95 = sorted(samples)[int(len(samples) * 0.95) - 1]
        self.assertLess(p95, 0.250)

    def test_capacity_failure_records_receipt_before_provider_dispatch(self):
        root, home, workspace, previous = self.temp_context()
        provider_calls = 0
        events: list[tuple[str, dict]] = []

        async def measure(_args):
            return {
                "inputTokens": 202_261,
                "source": "fixture",
                "measuredAt": "2026-07-26T00:00:00Z",
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "estimatedFixedInputTokens": 20_000,
                "estimatedMessageInputTokens": 182_261,
                "estimatedImageInputTokens": 0,
            }

        async def skip_compaction(_args):
            return {"status": "skipped", "reason": "no_legal_cutoff"}

        async def send(_request):
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("capacity-exhausted request dispatched")

        try:
            config = run(
                core.resolveConfig(
                    {
                        "provider": "openai",
                        "runtimeHome": str(home),
                        "permissionMode": "bypassPermissions",
                    },
                    str(workspace),
                )
            )
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="bypassPermissions",
            )
            with (
                mock.patch.object(
                    context_module,
                    "measureRequestInputTokens",
                    measure,
                ),
                mock.patch.object(
                    context_module,
                    "maybeAutoCompactSession",
                    skip_compaction,
                ),
                self.assertRaises(core.ProviderRequestError) as raised,
            ):
                run(
                    core.runAgentPrompt(
                        session=session,
                        config=config,
                        runtime=bind_test_runtime(
                            session,
                            {
                                "emitEvent": lambda event, data: events.append(
                                    (event, data)
                                )
                            },
                        ),
                        prompt="protected current request",
                        providerOverride={"send": send},
                    )
                )
            self.assertEqual(
                raised.exception.record["code"], "context_capacity_exhausted"
            )
            self.assertEqual(provider_calls, 0)
            preflight = session["metadata"]["tokenTracking"]["lastPreflight"]
            self.assertEqual(
                preflight["requestPreflight"]["admission"]["status"],
                "context_capacity_exhausted",
            )
            self.assertTrue(
                any(event == "provider.request_preflight" for event, _ in events)
            )
        finally:
            self.restore_home(root, previous)
