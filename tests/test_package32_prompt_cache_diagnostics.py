from __future__ import annotations

from datetime import datetime, timezone

from tests.support.agent_core_case import *

from agent_core.providers.anthropic_parts.payloads import planAnthropicCacheBreakpoints
from agent_core.provider_prompt_cache import resolvePromptCachePreparation
from agent_core.providers.prompt_cache_diagnostics import (
    MAX_PROMPT_CACHE_STATE_BYTES,
    buildPromptCacheRequestSnapshot,
    canonicalPromptCacheJson,
    diagnosePromptCache,
    hashPromptCacheValue,
    normalizePromptCacheProviderReason,
)
from agent_core.shell_surface.status import formatUsageBreakdown, formatUsageSummary
from agent_core.usage_costs import (
    cacheCreationUsageDetail,
    createUsageTotals,
    ensureUsageTotals,
    estimateUsageCost,
    mergeProviderUsage,
    mergeUsageIntoSession,
    roundUsd,
)


def _policy(**overrides):
    return {
        "enabled": True,
        "mode": "explicit",
        "ttl": "5m",
        "scope": "ephemeral",
        "compatibility": "official",
        "providerDiagnostics": False,
        **overrides,
    }


def _messages(count, prefix="message"):
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": [{"type": "text", "text": f"{prefix}-{index}"}],
        }
        for index in range(count)
    ]


def _tools(count=2):
    return [
        {
            "name": f"secret-tool-{index}",
            "description": f"secret-description-{index}",
            "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}},
        }
        for index in range(count)
    ]


def _snapshot(**overrides):
    policy = overrides.get("policy") or _policy()
    messages = overrides.get("messages") or _messages(4)
    tools = overrides.get("tools") if "tools" in overrides else _tools()
    system = overrides.get("system", "secret-system")
    request = {
        "model": overrides.get("model", "claude-sonnet-4-6"),
        "system": system,
        "messages": messages,
        "tools": tools,
        "promptCache": policy,
    }
    plan = planAnthropicCacheBreakpoints(request)
    return buildPromptCacheRequestSnapshot(
        {
            "providerProfileName": overrides.get("providerProfileName", "anthropic-console"),
            "routeClass": overrides.get("routeClass", "official"),
            "baseUrl": overrides.get("baseUrl"),
            "model": request["model"],
            "betaTokens": overrides.get("betaTokens", ["cache-diagnosis-2026-04-07"]),
            "normalizedTools": tools,
            "normalizedSystem": [{"type": "text", "text": "billing"}, {"type": "text", "text": system}],
            "normalizedMessages": messages,
            "toolChoice": overrides.get("toolChoice"),
            "outputConfig": overrides.get("outputConfig"),
            "policy": policy,
            "plan": plan,
            "preparation": overrides.get("preparation", "normal"),
            "reactiveAttempt": overrides.get("reactiveAttempt", 0),
        }
    )


class PromptCacheDiagnosticsTests(AgentCoreTestCase):
    def test_preparation_provenance_matrix_is_closed_and_priority_ordered(self):
        cases = (
            ({}, "normal"),
            ({"durableCompactionApplied": True}, "compacted"),
            ({"continuationReliefApplied": True}, "compacted"),
            ({"reactiveAttempt": 1, "durableCompactionApplied": True}, "reactive_relief"),
            ({"repaired": True, "reactiveAttempt": 2, "durableCompactionApplied": True}, "repaired"),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(resolvePromptCachePreparation(args), expected)

    def test_canonical_hashing_and_bounded_snapshot_are_content_free(self):
        left = {"z": 3, "nested": {"b": 2, "a": 1}, "array": [3, 2, 1]}
        right = {"array": [3, 2, 1], "nested": {"a": 1, "b": 2}, "z": 3}
        self.assertEqual(canonicalPromptCacheJson(left), canonicalPromptCacheJson(right))
        self.assertEqual(hashPromptCacheValue("one", left), hashPromptCacheValue("one", right))
        self.assertNotEqual(hashPromptCacheValue("one", left), hashPromptCacheValue("two", left))
        snapshot = _snapshot(
            messages=_messages(180, "PRIVATE_MESSAGE_SENTINEL"),
            tools=_tools(180),
            system="PRIVATE_SYSTEM_SENTINEL",
            model="PRIVATE_MODEL_SENTINEL",
            providerProfileName="PRIVATE_PROFILE_SENTINEL",
            baseUrl="https://user:pass@example.test/v1?secret=PRIVATE_QUERY_SENTINEL",
            betaTokens=["PRIVATE_BETA_SENTINEL"],
        )
        serialized = json.dumps(snapshot)
        for sentinel in (
            "PRIVATE_MESSAGE_SENTINEL", "PRIVATE_SYSTEM_SENTINEL", "secret-tool-", "secret-description-",
            "PRIVATE_MODEL_SENTINEL", "PRIVATE_PROFILE_SENTINEL", "PRIVATE_QUERY_SENTINEL", "PRIVATE_BETA_SENTINEL", "user:pass",
        ):
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel, serialized)
        self.assertLess(len(serialized.encode("utf-8")), MAX_PROMPT_CACHE_STATE_BYTES)
        self.assertEqual(len(snapshot["boundedMessageHashes"]), 64)
        self.assertEqual(len(snapshot["boundedToolHashes"]), 128)
        self.assertIn("omittedPrefixHash", snapshot)

    def test_diagnosis_reason_precedence_covers_provider_visible_axes(self):
        baseline = _snapshot()
        cases = (
            ("provider_route_changed", {**baseline, "routeClass": "compatible"}),
            ("model_changed", {**baseline, "modelHash": "b" * 64}),
            ("cache_policy_changed", {**baseline, "cachePolicyHash": "b" * 64}),
            ("beta_set_changed", {**baseline, "betasHash": "b" * 64}),
            ("tools_changed", {**baseline, "toolsHash": "b" * 64}),
            ("system_changed", {**baseline, "systemHash": "b" * 64}),
            ("tool_choice_changed", {**baseline, "toolChoiceHash": "b" * 64}),
            ("thinking_or_output_config_changed", {**baseline, "outputConfigHash": "b" * 64}),
        )
        for reason, current in cases:
            with self.subTest(reason=reason):
                self.assertEqual(diagnosePromptCache({"previous": baseline, "current": current, "ttl": "5m"})["reason"], reason)
        all_changed = {
            **baseline,
            "schemaVersion": 2,
            "routeClass": "compatible",
            "modelHash": "b" * 64,
        }
        self.assertEqual(
            diagnosePromptCache({"previous": baseline, "current": all_changed, "ttl": "5m"})["reason"],
            "state_schema_changed",
        )

    def test_rewrite_provenance_usage_and_ttl_classification(self):
        previous = _snapshot(messages=_messages(2), tools=[])
        appended_policy = _policy(messageTailPreviousAnchor=previous["tailA"])
        appended = _snapshot(messages=_messages(4), tools=[], policy=appended_policy)
        self.assertEqual(appended["tailB"]["identity"], previous["tailA"]["identity"])
        self.assertEqual(diagnosePromptCache({"previous": previous, "current": appended, "usage": {"cache_read_input_tokens": 10}, "ttl": "5m"})["reason"], "cache_read_observed")
        rewritten = _messages(4)
        rewritten[0]["content"][0]["text"] = "rewritten"
        current = _snapshot(messages=rewritten, tools=[], policy=appended_policy)
        self.assertEqual(diagnosePromptCache({"previous": previous, "current": current, "ttl": "5m"})["reason"], "anchor_identity_changed")
        compacted = {**current, "preparation": "compacted"}
        self.assertEqual(diagnosePromptCache({"previous": previous, "current": compacted, "ttl": "5m"})["reason"], "compaction_or_relief_rewrite")
        unchanged = _snapshot()
        before = "2026-07-17T00:00:00Z"
        self.assertEqual(diagnosePromptCache({"previous": unchanged, "current": unchanged, "usage": {}, "previousCommittedAt": before, "now": datetime(2026, 7, 17, 0, 6, tzinfo=timezone.utc), "ttl": "5m"})["reason"], "ttl_5m_may_have_expired")
        self.assertEqual(diagnosePromptCache({"previous": unchanged, "current": unchanged, "usage": {}, "previousCommittedAt": before, "now": datetime(2026, 7, 17, 1, 1, tzinfo=timezone.utc), "ttl": "1h"})["reason"], "ttl_1h_may_have_expired")

    def test_provider_reason_matrix_is_closed(self):
        cases = (
            ({"requested": False, "fieldPresent": False}, "not_requested"),
            ({"requested": True, "previousMessageIdSent": True, "fieldPresent": False}, "unavailable"),
            ({"requested": True, "previousMessageIdSent": False, "fieldPresent": True, "diagnostics": None}, "warmup_no_comparison"),
            ({"requested": True, "previousMessageIdSent": True, "fieldPresent": True, "diagnostics": None}, "no_divergence"),
            ({"requested": True, "previousMessageIdSent": True, "fieldPresent": True, "diagnostics": {"cache_miss_reason": None}}, "comparison_pending"),
            ({"requested": True, "previousMessageIdSent": True, "fieldPresent": True, "diagnostics": {"cache_miss_reason": {"type": "tools_changed"}}}, "tools_changed"),
            ({"requested": True, "previousMessageIdSent": True, "fieldPresent": True, "diagnostics": {"cache_miss_reason": {"type": "secret_future_reason"}}}, "unknown"),
            ({"requested": True, "previousMessageIdSent": True, "fieldPresent": True, "diagnostics": "malformed"}, "unavailable"),
            ({"requested": True, "previousMessageIdSent": True, "fieldPresent": True, "diagnostics": {}}, "unavailable"),
        )
        for args, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(normalizePromptCacheProviderReason(args), expected)

    def test_accumulation_pricing_and_reports_use_complete_detail_once(self):
        aggregate = {}
        mergeProviderUsage(aggregate, {"cache_creation_input_tokens": 100, "cache_creation_5m_input_tokens": 100})
        mergeProviderUsage(aggregate, {"cache_creation_input_tokens": 200, "cache_creation_1h_input_tokens": 200})
        self.assertEqual(cacheCreationUsageDetail(aggregate), {"aggregate": 300, "fiveMinutes": 100, "oneHour": 200, "present": True, "reconciled": True, "complete": True})
        session = {"metadata": {}}
        self.assertEqual(ensureUsageTotals(session), createUsageTotals())
        mergeUsageIntoSession(session, aggregate)
        totals = session["metadata"]["usage"]
        self.assertTrue(totals["cacheCreationTtlDetailComplete"])
        self.assertIn("written: 5m 100, 1h 200", formatUsageSummary(totals))
        self.assertIn("cache written by TTL", formatUsageBreakdown(totals))
        mergeUsageIntoSession(session, {"cache_creation_input_tokens": 1})
        self.assertFalse(totals["cacheCreationTtlDetailComplete"])
        self.assertNotIn("written: 5m", formatUsageSummary(totals))
        legacy = {
            "metadata": {
                "usage": {
                    "turns": 1, "toolCalls": 2, "inputTokens": 3,
                    "outputTokens": 4, "cacheCreationInputTokens": 5,
                    "cacheReadInputTokens": 6,
                }
            }
        }
        normalized = ensureUsageTotals(legacy)
        self.assertEqual(normalized["cacheCreation5mInputTokens"], 0)
        self.assertEqual(normalized["cacheCreation1hInputTokens"], 0)
        self.assertFalse(normalized["cacheCreationTtlDetailComplete"])
        detailed = estimateUsageCost({"cache_creation_input_tokens": 1_000_000, "cache_creation_5m_input_tokens": 500_000, "cache_creation_1h_input_tokens": 500_000}, "claude-sonnet-4-6")
        fallback = estimateUsageCost({"cache_creation_input_tokens": 1_000_000, "cache_creation_5m_input_tokens": 600_000, "cache_creation_1h_input_tokens": 600_000}, "claude-sonnet-4-6")
        self.assertEqual(detailed["cacheWriteUsd"], 4.875)
        self.assertEqual(fallback["cacheWriteUsd"], 3.75)
        self.assertTrue(fallback["cacheWriteEstimated"])

    def test_usage_detail_shape_matrix_never_synthesizes_or_double_charges(self):
        cases = (
            ({}, True, 0, False),
            ({"cache_creation_input_tokens": 0}, True, 0, False),
            ({"cache_creation_input_tokens": 100, "cache_creation_5m_input_tokens": 100}, True, 3.75e-4, False),
            ({"cache_creation_input_tokens": 100, "cache_creation_1h_input_tokens": 100}, True, 6e-4, False),
            ({"cache_creation_input_tokens": 100, "cache_creation_5m_input_tokens": 40, "cache_creation_1h_input_tokens": 60}, True, 5.1e-4, False),
            ({"cache_creation_input_tokens": 100}, False, 3.75e-4, True),
            ({"cache_creation_input_tokens": 100, "cache_creation_5m_input_tokens": 60, "cache_creation_1h_input_tokens": 60}, False, 3.75e-4, True),
            ({"cache_creation_input_tokens": 100, "cache_creation_5m_input_tokens": 100, "cache_creation_ttl_detail_complete": False}, False, 3.75e-4, True),
            ({"cache_creation_input_tokens": 100, "cache_creation_5m_input_tokens": -1, "cache_creation_1h_input_tokens": math.inf}, False, 3.75e-4, True),
        )
        for usage, complete, expected_cost, estimated in cases:
            with self.subTest(usage=usage):
                detail = cacheCreationUsageDetail(usage)
                cost = estimateUsageCost(usage, "claude-sonnet-4-6")
                self.assertEqual(detail["complete"], complete)
                self.assertAlmostEqual(cost["cacheWriteUsd"], expected_cost)
                self.assertEqual(cost["cacheWriteEstimated"], estimated)

    def test_usd_rounding_matches_typescript_math_round_at_half_boundary(self):
        self.assertEqual(roundUsd(0.0000325), 0.000033)
        self.assertEqual(roundUsd(0.000032499999), 0.000032)
        self.assertEqual(roundUsd(-0.0000325), -0.000032)


if __name__ == "__main__":
    unittest.main()
