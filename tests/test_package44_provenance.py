from __future__ import annotations

from typing import Any

import tess_core._facade as core
from tess_core.context.compaction.models import _extract_recent_user_requests
from tess_core.context.request_preflight import _request_fingerprint
from tess_core.storage.message_origin import (
    is_genuine_user_request,
    is_plain_user_turn_anchor,
    message_with_origin,
    synthetic_user_message,
    user_request_message,
)
from tess_core.providers.token_budget import resolve_interactive_context_policy
from tests.support.tess_core_case import TessCoreTestCase


class Package44ProvenanceTests(TessCoreTestCase):
    def test_removed_settings_are_inert_and_reported(self):
        policy = resolve_interactive_context_policy(
            {
                "profileName": "codex",
                "kind": "openai",
                "apiMode": "codex",
                "contextReliefThresholdTokens": "broken historical value",
                "contextReliefTargetTokens": -1,
                "inlineToolResultTurnBytes": "also historical",
            }
        )
        self.assertIsNone(policy["contextReliefThresholdTokens"])
        self.assertIsNone(policy["contextReliefTargetTokens"])
        self.assertIsNone(policy["inlineToolResultTurnBytes"])
        self.assertEqual(policy["reliefSource"], "deprecated_ignored")
        self.assertIsNone(policy["inlineToolResultBatchBytes"])
        report = core.build_provider_profiles_report(
            {"providerProfileName": "codex"},
            {
                "providerProfiles": {
                    "codex": {
                        "kind": "openai",
                        "apiMode": "codex",
                        "contextReliefThresholdTokens": 64_000,
                        "contextReliefTargetTokens": 48_000,
                        "inlineToolResultTurnBytes": 64_000,
                    }
                }
            },
        )
        self.assertIn(
            "interactive relief and turn budget settings are ignored",
            report,
        )

    def test_synthetic_messages_never_acquire_request_authority(self):
        genuine = user_request_message("Fix the current production issue.")
        bridge = {"role": "user", "content": core.CONTINUATION_BRIDGE_MESSAGE}
        partial = synthetic_user_message(
            "Partial continuation summary for older raw context in this session:\n"
            "Carry-forward requests:\n- stale synthetic objective",
            kind="partial_compaction_summary",
        )
        task = synthetic_user_message(
            "[PARENT MESSAGE]\nkind: guidance\nmessage_id: msg-1\n\n"
            "synthetic guidance\n[/PARENT MESSAGE]",
            kind="task_notification",
        )
        messages = [genuine, bridge, partial, task]
        self.assertTrue(is_genuine_user_request(genuine))
        self.assertTrue(
            is_genuine_user_request(
                user_request_message(core.CONTINUATION_BRIDGE_MESSAGE)
            )
        )
        self.assertTrue(
            all(not is_genuine_user_request(message) for message in messages[1:])
        )
        self.assertEqual(
            _extract_recent_user_requests(messages),
            ["Fix the current production issue."],
        )
        self.assertEqual(core.find_last_plain_user_index(messages), 0)
        self.assertEqual(
            core.summarize_continuation_region(messages)["requests"],
            ["Fix the current production issue."],
        )
        synthesis = core.build_evidence_backed_compaction_synthesis(
            {
                "sourceStart": 0,
                "sourceMessages": messages,
                "previousWorkingContext": {},
                "reason": "deterministic fallback",
                "evidence": {
                    "transcript": "",
                    "includedEntryCount": 1,
                    "entryCount": 1,
                    "tokenEstimate": 1,
                    "tokenBudget": 32,
                },
                "evidenceBudgetTokens": 32,
                "model": {"provider": "openai", "model": "gpt-5.6-sol"},
            }
        )
        self.assertEqual(
            synthesis["workingContext"]["goals"]["active"],
            ["Fix the current production issue."],
        )

    def test_runtime_compaction_messages_carry_origin(self):
        bridged = core.build_messages_for_working_context(
            {
                "messages": [
                    user_request_message("Original objective."),
                    {"role": "assistant", "content": "working"},
                ],
                "metadata": {
                    "compactionState": {"compactedThroughMessageIndex": 1}
                },
            }
        )
        self.assertEqual(
            bridged[0]["metadata"]["tessCoreOrigin"]["kind"],
            "continuation_bridge",
        )
        messages: list[dict[str, Any]] = [
            (
                user_request_message(f"request {index}")
                if index % 2 == 0
                else {"role": "assistant", "content": f"finding {index}"}
            )
            for index in range(30)
        ]
        summary = core.apply_partial_compaction(messages)["messages"][0]
        self.assertEqual(
            summary["metadata"]["tessCoreOrigin"]["kind"],
            "partial_compaction_summary",
        )
        self.assertFalse(is_genuine_user_request(summary))

    def test_origin_copy_isolated_and_no_progress_event_typed(self):
        original = user_request_message("hello")
        copied = message_with_origin(
            original,
            actor="runtime",
            kind="test",
            synthetic=True,
        )
        self.assertTrue(is_genuine_user_request(original))
        self.assertFalse(is_genuine_user_request(copied))
        self.assertNotEqual(original["metadata"], copied["metadata"])
        event = core.create_runtime_event(
            {
                "event": "agent.no_progress",
                "data": {
                    "session_id": "session-1",
                    "logical_turn_id": "turn-1",
                    "reason": "artifact_output_unreachable",
                    "artifactId": "artifact-1",
                    "providerCallIndex": 7,
                    "toolBatchIndex": 6,
                    "consecutiveNoProgress": 3,
                },
            }
        )
        self.assertEqual(event["kind"], "agent.no_progress")
        self.assertEqual(event["payload"]["reason"], "artifact_output_unreachable")
        self.assertEqual(event["payload"]["artifactId"], "artifact-1")

    def test_explicit_delegated_task_authority_survives_synthetic_authorship(self):
        delegated = message_with_origin(
            {"role": "user", "content": "Investigate the child task."},
            actor="subagent",
            kind="subagent_task_prompt",
            intentAuthority="request",
            synthetic=True,
        )
        self.assertTrue(is_genuine_user_request(delegated))
        self.assertTrue(is_plain_user_turn_anchor(delegated))
        self.assertEqual(
            core.summarize_continuation_region([delegated])["requests"],
            ["Investigate the child task."],
        )

    def test_request_fingerprint_ignores_volatile_origin_lineage_only(self):
        first = {
            "model": "gpt-5.6-sol",
            "messages": [
                user_request_message(
                    "same request",
                    lineage={"logicalTurnId": "turn-random-a"},
                )
            ],
        }
        second = {
            "model": "gpt-5.6-sol",
            "messages": [
                user_request_message(
                    "same request",
                    lineage={"logicalTurnId": "turn-random-b"},
                )
            ],
        }
        self.assertEqual(_request_fingerprint(first), _request_fingerprint(second))
        second["messages"][0]["metadata"]["tessCoreOrigin"]["kind"] = "other"
        self.assertNotEqual(_request_fingerprint(first), _request_fingerprint(second))
