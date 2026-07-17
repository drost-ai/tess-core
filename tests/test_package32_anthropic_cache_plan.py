from __future__ import annotations

import copy

from tests.support.agent_core_case import *

from agent_core.providers.anthropic_cache import countAnthropicCacheControls
from agent_core.providers.anthropic_parts.payloads import (
    buildAnthropicBetaHeader,
    buildAnthropicCountTokensPayload,
    buildAnthropicHeaders,
    buildAnthropicMessagesEnvelope,
    buildAnthropicMessagesPayload,
    planAnthropicCacheBreakpoints,
)
from agent_core.providers.anthropic_parts.transport import applyAnthropicUsageSnapshot
from agent_core.usage_costs import estimateUsageCost, resolveModelPricing
from agent_core.providers.prompt_cache_diagnostics import normalizePromptCacheProviderReason


def _policy(**overrides):
    return {
        "enabled": True,
        "mode": "explicit",
        "ttl": "1h",
        "compatibility": "official",
        "providerDiagnostics": False,
        "scope": "ephemeral",
        **overrides,
    }


def _messages(count):
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": [{"type": "text", "text": f"message-{index}"}],
        }
        for index in range(count)
    ]


def _tools():
    return [
        {
            "name": "first",
            "description": "first",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "last",
            "description": "last",
            "input_schema": {
                "type": "object",
                "properties": {"cache_control": {"type": "string"}},
            },
        },
    ]


def _request(*, messages=None, tools=None, prompt_cache=None):
    return {
        "model": "claude-sonnet-4-6",
        "system": "stable system",
        "messages": _messages(4) if messages is None else messages,
        "tools": _tools() if tools is None else tools,
        "promptCache": _policy() if prompt_cache is None else prompt_cache,
    }


class AnthropicCachePlanTests(AgentCoreTestCase):
    def test_exhaustive_allocator_matrix_matches_wire_and_priority(self):
        for mode in ("off", "explicit", "auto"):
            for has_system in (False, True):
                for has_tools in (False, True):
                    for message_count in (0, 2, 4):
                        for reuse_previous in (False, True):
                            prior = None
                            if reuse_previous and message_count >= 2:
                                prior = planAnthropicCacheBreakpoints(
                                    _request(messages=_messages(2), tools=_tools() if has_tools else [], prompt_cache=_policy(mode="explicit"))
                                )["tailA"]
                            policy = _policy(
                                mode=mode,
                                enabled=mode != "off",
                                messageTailPreviousAnchor=prior,
                            )
                            request = _request(
                                messages=_messages(message_count),
                                tools=_tools() if has_tools else [],
                                prompt_cache=policy,
                            )
                            request["system"] = "stable system" if has_system else ""
                            with self.subTest(mode=mode, system=has_system, tools=has_tools, messages=message_count, reuse=reuse_previous):
                                plan = planAnthropicCacheBreakpoints(request)
                                payload = buildAnthropicMessagesPayload(request)
                                self.assertLessEqual(plan["usedBreakpoints"], 4)
                                self.assertEqual(countAnthropicCacheControls(payload), plan["usedBreakpoints"])
                                expected_order = [name for name in ("automatic", "system", "tools", "tail_a", "tail_b") if name in plan["placements"]]
                                self.assertEqual(plan["placements"], expected_order)

    def test_four_slot_priority_and_automatic_displacement(self):
        previous = planAnthropicCacheBreakpoints(
            _request(messages=_messages(2), prompt_cache=_policy(mode="auto"))
        )["tailA"]
        request = _request(
            prompt_cache=_policy(mode="auto", messageTailPreviousAnchor=previous)
        )
        plan = planAnthropicCacheBreakpoints(request)
        payload = buildAnthropicMessagesPayload(request)
        self.assertEqual(plan["placements"], ["automatic", "system", "tools", "tail_a"])
        self.assertIsNone(plan["tailB"])
        self.assertEqual(plan["omitted"], [{"placement": "tail_b", "reason": "budget"}])
        self.assertEqual(plan["usedBreakpoints"], 4)
        self.assertEqual(countAnthropicCacheControls(payload), 4)

    def test_explicit_allocates_system_tools_a_and_identity_valid_b(self):
        previous = planAnthropicCacheBreakpoints(_request(messages=_messages(2)))["tailA"]
        request = _request(prompt_cache=_policy(messageTailPreviousAnchor=previous))
        plan = planAnthropicCacheBreakpoints(request)
        payload = buildAnthropicMessagesPayload(request)
        self.assertEqual(plan["placements"], ["system", "tools", "tail_a", "tail_b"])
        self.assertEqual((plan["tailA"]["messageIndex"], plan["tailB"]["messageIndex"]), (2, 0))
        self.assertEqual(countAnthropicCacheControls(payload), 4)
        self.assertEqual(payload["tools"][-1]["cache_control"], {"type": "ephemeral", "ttl": "1h"})

    def test_without_tools_b_uses_released_automatic_slot(self):
        previous = planAnthropicCacheBreakpoints(
            _request(messages=_messages(2), tools=[], prompt_cache=_policy(mode="auto"))
        )["tailA"]
        plan = planAnthropicCacheBreakpoints(
            _request(tools=[], prompt_cache=_policy(mode="auto", messageTailPreviousAnchor=previous))
        )
        self.assertEqual(plan["placements"], ["automatic", "system", "tail_a", "tail_b"])

    def test_legal_block_selection_skips_thinking_empty_and_unknown(self):
        legal = [
            {"type": "text", "text": "eligible"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
            {"type": "tool_use", "id": "u", "name": "Read", "input": {}},
            {"type": "tool_result", "tool_use_id": "u", "content": "ok"},
        ]
        illegal = [
            {"type": "text", "text": ""},
            {"type": "thinking", "thinking": "private"},
            {"type": "future", "value": "unknown"},
        ]
        for block in legal + illegal:
            with self.subTest(kind=block["type"], legal=block in legal):
                plan = planAnthropicCacheBreakpoints(
                    _request(messages=[{"role": "user", "content": [block]}, {"role": "user", "content": "volatile"}], tools=[])
                )
                self.assertEqual(plan["tailA"] is not None, block in legal)

        transcript = [
            {"role": "user", "content": "stable"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "eligible"},
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": ""},
                ],
            },
            {"role": "user", "content": "volatile"},
        ]
        plan = planAnthropicCacheBreakpoints(_request(messages=transcript, tools=[]))
        self.assertEqual((plan["tailA"]["messageIndex"], plan["tailA"]["blockIndex"]), (1, 0))

    def test_identity_rejects_same_coordinate_after_earlier_rewrite(self):
        previous = planAnthropicCacheBreakpoints(_request(messages=_messages(2), tools=[]))["tailA"]
        rewritten = _messages(4)
        rewritten[0]["content"][0]["text"] = "rewritten-prefix"
        plan = planAnthropicCacheBreakpoints(
            _request(messages=rewritten, tools=[], prompt_cache=_policy(messageTailPreviousAnchor=previous))
        )
        self.assertIsNone(plan["tailB"])
        self.assertEqual(plan["omitted"], [{"placement": "tail_b", "reason": "identity_mismatch"}])

    def test_previous_anchor_omission_reasons_are_closed_and_identity_bound(self):
        previous = planAnthropicCacheBreakpoints(_request(messages=_messages(2), tools=[]))["tailA"]
        changed_block = _messages(4)
        changed_block[previous["messageIndex"]]["content"][previous["blockIndex"]]["text"] = "changed-block"
        cases = (
            (previous, changed_block, "identity_mismatch"),
            ({**previous, "messageIndex": 99}, _messages(4), "out_of_range"),
            (previous, [
                {"role": "user", "content": [{"type": "text", "text": ""}]},
                *_messages(3)[1:],
            ], "illegal_block"),
        )
        for anchor, messages, reason in cases:
            with self.subTest(reason=reason):
                plan = planAnthropicCacheBreakpoints(
                    _request(
                        messages=messages,
                        tools=[],
                        prompt_cache=_policy(messageTailPreviousAnchor=anchor),
                    )
                )
                self.assertIsNone(plan["tailB"])
                self.assertEqual(plan["omitted"], [{"placement": "tail_b", "reason": reason}])

        current = planAnthropicCacheBreakpoints(_request(messages=_messages(4), tools=[]))["tailA"]
        same_coordinate = planAnthropicCacheBreakpoints(
            _request(
                messages=_messages(4), tools=[],
                prompt_cache=_policy(messageTailPreviousAnchor=current),
            )
        )
        self.assertEqual(
            same_coordinate["omitted"],
            [{"placement": "tail_b", "reason": "not_strict_prefix"}],
        )

    def test_marker_application_does_not_mutate_registry(self):
        tools = _tools()
        before = copy.deepcopy(tools)
        payload = buildAnthropicMessagesPayload(_request(tools=tools))
        self.assertEqual(tools, before)
        self.assertNotIn("cache_control", payload["tools"][0])
        self.assertIn("cache_control", payload["tools"][-1])

    def test_count_payload_removes_authority_but_preserves_user_fields(self):
        payload = buildAnthropicCountTokensPayload(
            _request(
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "u",
                                "name": "Read",
                                "input": {"cache_control": {"user_owned": True}},
                            }
                        ],
                    },
                    {"role": "user", "content": "volatile"},
                ]
            )
        )
        self.assertEqual(countAnthropicCacheControls(payload), 0)
        self.assertEqual(
            payload["tools"][-1]["input_schema"]["properties"]["cache_control"],
            {"type": "string"},
        )
        self.assertEqual(
            payload["messages"][0]["content"][0]["input"]["cache_control"],
            {"user_owned": True},
        )

    def test_compatible_tool_and_tail_breakpoints_require_opt_in(self):
        default = buildAnthropicMessagesPayload(
            _request(prompt_cache=_policy(compatibility="compatible"))
        )
        self.assertNotIn("cache_control", default["tools"][-1])
        self.assertNotIn("cache_control", default["messages"][2]["content"][0])
        opted = buildAnthropicMessagesPayload(
            _request(
                prompt_cache=_policy(
                    compatibility="compatible",
                    toolDefinitionBreakpoint=True,
                    messageTailBreakpoints=True,
                )
            )
        )
        self.assertIn("cache_control", opted["tools"][-1])
        self.assertIn("cache_control", opted["messages"][2]["content"][0])

    def test_feature_overrides_and_suppressed_routes_control_authority(self):
        cases = (
            (_policy(enabled=False), 0),
            (_policy(mode="explicit", explicit=False), 0),
            (_policy(mode="auto", explicit=False), 1),
            (_policy(messageTailBreakpoints=False), 2),
            (_policy(toolDefinitionBreakpoint=False), 2),
            (_policy(compatibility="claude_code", compatibilitySuppressedReason="suppressed"), 0),
        )
        for policy, expected in cases:
            with self.subTest(policy=policy):
                payload = buildAnthropicMessagesPayload(_request(prompt_cache=policy))
                self.assertEqual(countAnthropicCacheControls(payload), expected)

    def test_cross_provider_24h_ttl_is_never_serialized_to_anthropic(self):
        payload = buildAnthropicMessagesPayload(
            _request(prompt_cache=_policy(ttl="24h"))
        )
        controls = []
        controls.extend(block.get("cache_control") for block in payload.get("system", []) if block.get("cache_control"))
        controls.extend(tool.get("cache_control") for tool in payload.get("tools", []) if tool.get("cache_control"))
        self.assertTrue(controls)
        self.assertTrue(all(control == {"type": "ephemeral"} for control in controls))

    def test_beta_set_merge_is_deterministic_and_protected(self):
        config = {
            "apiKey": "real-key",
            "customHeaders": {
                "x-extra": "ok",
                "Anthropic-Beta": " Custom-Beta, CACHE-DIAGNOSIS-2026-04-07 ",
                "anthropic-beta": "custom-beta,second-beta",
                "Authorization": "attacker",
                "X-API-Key": "attacker-key",
            },
        }
        beta = buildAnthropicBetaHeader(config, {"providerDiagnostics": True})
        self.assertEqual(
            beta,
            "fine-grained-tool-streaming-2025-05-14,cache-diagnosis-2026-04-07,Custom-Beta,second-beta",
        )
        headers = buildAnthropicHeaders(config, features={"providerDiagnostics": True})
        self.assertEqual(headers["anthropic-beta"], beta)
        self.assertEqual(headers["x-api-key"], "real-key")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("X-API-Key", headers)
        self.assertEqual(headers["x-extra"], "ok")

        oauth = buildAnthropicHeaders(
            {
                "authToken": "oauth-token",
                "customHeaders": {
                    "authorization": "Bearer attacker",
                    "X-API-Key": "attacker-key",
                    "ANTHROPIC-BETA": "custom-oauth",
                },
            }
        )
        self.assertEqual(oauth["authorization"], "Bearer oauth-token")
        self.assertFalse(any(name.lower() == "x-api-key" for name in oauth))
        self.assertEqual(sum(name.lower() == "anthropic-beta" for name in oauth), 1)
        self.assertIn("custom-oauth", oauth["anthropic-beta"])

    def test_usage_detail_parses_nested_fields_and_survives_sparse_delta(self):
        usage = {}
        applyAnthropicUsageSnapshot(
            usage,
            {
                "input_tokens": 100,
                "cache_creation_input_tokens": 30,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 10,
                    "ephemeral_1h_input_tokens": 20,
                },
            },
        )
        applyAnthropicUsageSnapshot(usage, {"output_tokens": 7})
        self.assertEqual(usage["cache_creation_5m_input_tokens"], 10)
        self.assertEqual(usage["cache_creation_1h_input_tokens"], 20)
        self.assertTrue(usage["cache_creation_ttl_detail_complete"])
        self.assertEqual(usage["output_tokens"], 7)

    def test_ttl_pricing_uses_detail_or_single_five_minute_fallback(self):
        detailed = estimateUsageCost(
            {
                "cache_creation_input_tokens": 30,
                "cache_creation_5m_input_tokens": 10,
                "cache_creation_1h_input_tokens": 20,
                "cache_creation_ttl_detail_complete": True,
            },
            "claude-opus-4-8-20260701",
        )
        self.assertEqual(resolveModelPricing("claude-opus-4-8-20260701")["cacheWrite1hUsdPerMtok"], 10)
        self.assertAlmostEqual(detailed["cacheWriteUsd"], 0.0002625)
        self.assertFalse(detailed["cacheWriteEstimated"])
        fallback = estimateUsageCost(
            {"cache_creation_input_tokens": 30}, "claude-opus-4-8"
        )
        self.assertAlmostEqual(fallback["cacheWriteUsd"], 0.0001875)
        self.assertTrue(fallback["cacheWriteEstimated"])

    def test_diagnostics_envelope_reuses_only_matching_previous_id(self):
        dispatch = {
            "sessionId": "session-diag",
            "paths": None,
            "dispatchOrder": {
                "runtimeGeneration": 1,
                "messageCount": 4,
                "reactiveAttempt": 0,
                "startedAtMs": 1,
                "localSequence": 1,
                "nonce": "nonce",
            },
            "preparation": "normal",
        }
        request = _request(
            prompt_cache=_policy(providerDiagnostics=True, dispatchContext=dispatch)
        )
        config = {
            "provider": "anthropic",
            "providerProfileName": "official",
            "apiKey": "test-key",
            "customHeaders": {},
        }
        warmup = buildAnthropicMessagesEnvelope(request, config)
        self.assertEqual(warmup["payload"]["diagnostics"], {"previous_message_id": None})
        self.assertIn("cache-diagnosis-2026-04-07", warmup["headerFeatures"] and buildAnthropicBetaHeader(config, warmup["headerFeatures"]))
        request["promptCache"]["dispatchContext"]["previous"] = {
            "request": warmup["promptCache"]["snapshot"],
            "responseId": "msg_previous",
        }
        hit = buildAnthropicMessagesEnvelope(request, config)
        self.assertEqual(hit["payload"]["diagnostics"], {"previous_message_id": "msg_previous"})
        self.assertTrue(hit["diagnostics"]["previousMessageIdSent"])
        request["model"] = "claude-opus-4-8"
        mismatch = buildAnthropicMessagesEnvelope(request, config)
        self.assertEqual(mismatch["payload"]["diagnostics"], {"previous_message_id": None})

    def test_provider_diagnosis_is_closed_and_runtime_event_drops_identifiers(self):
        self.assertEqual(
            normalizePromptCacheProviderReason(
                {"requested": True, "previousMessageIdSent": True, "fieldPresent": True, "diagnostics": None}
            ),
            "no_divergence",
        )
        self.assertEqual(
            normalizePromptCacheProviderReason(
                {
                    "requested": True,
                    "previousMessageIdSent": True,
                    "fieldPresent": True,
                    "diagnostics": {"cache_miss_reason": {"type": "future_secret_reason"}},
                }
            ),
            "unknown",
        )
        event = core.createRuntimeEvent(
            {
                "event": "provider.prompt_cache",
                "data": {
                    "session_id": "session-secret",
                    "phase": "observed",
                    "mode": "auto",
                    "ttl": "1h",
                    "scope": "ephemeral",
                    "used_breakpoints": 99,
                    "placements": ["automatic", "system", "automatic", "bad", "tools", "tail_a", "tail_b"],
                    "cache_read_input_tokens": 12.9,
                    "local_reason": "model_changed",
                    "provider_reason": "future_secret_reason",
                    "response_id": "msg_secret",
                    "semantic_prefix_hash": "hash-secret",
                },
            }
        )
        self.assertEqual(event["kind"], "provider.prompt_cache")
        self.assertEqual(event["payload"]["usedBreakpoints"], 4)
        self.assertEqual(event["payload"]["placements"], ["automatic", "system", "tools", "tail_a"])
        self.assertEqual(event["payload"]["cacheReadInputTokens"], 12)
        self.assertIsNone(event["payload"]["providerReason"])
        self.assertNotIn("responseId", event["payload"])
        self.assertNotIn("semanticPrefixHash", event["payload"])
        runtime_state = core.createInitialRuntimeState()
        self.assertIs(core.reduceRuntimeState(runtime_state, event), runtime_state)
        self.assertIs(core.reduceRuntimeState(runtime_state, event), runtime_state)

    def test_custom_beta_tokens_cannot_activate_compatible_diagnostics_or_scope_fields(self):
        config = {
            "provider": "anthropic",
            "providerProfileName": "compatible",
            "anthropicBaseUrl": "https://compatible.example/v1",
            "apiKey": "test-key",
            "customHeaders": {
                "Anthropic-Beta": "cache-diagnosis-2026-04-07,cache-scope-future-token"
            },
        }
        request = _request(
            prompt_cache=_policy(
                compatibility="compatible",
                providerDiagnostics=True,
                scope="global",
                dispatchContext={
                    "sessionId": "compatible-session",
                    "paths": None,
                    "dispatchOrder": {"runtimeGeneration": 1, "messageCount": 4, "reactiveAttempt": 0, "startedAtMs": 1, "localSequence": 1, "nonce": "n"},
                    "preparation": "normal",
                },
            )
        )
        envelope = buildAnthropicMessagesEnvelope(request, config)
        self.assertFalse(envelope["diagnostics"]["requested"])
        self.assertNotIn("diagnostics", envelope["payload"])
        serialized = json.dumps(envelope["payload"])
        self.assertNotIn("global", serialized)
        self.assertNotIn("cache-scope", serialized)
        count = buildAnthropicCountTokensPayload(request, {"config": config})
        self.assertNotIn("diagnostics", count)


if __name__ == "__main__":
    unittest.main()
