from __future__ import annotations

from typing import Any

import agent_core as core
from agent_core.context.compaction.models import _extract_recent_user_requests
from agent_core.context.request_preflight import _request_fingerprint
from agent_core.session.message_origin import (
    isGenuineUserRequest,
    isPlainUserTurnAnchor,
    messageWithOrigin,
    syntheticUserMessage,
    userRequestMessage,
)
from agent_core.providers.token_budget import resolve_interactive_context_policy
from tests.support.agent_core_case import AgentCoreTestCase


class Package44ProvenanceTests(AgentCoreTestCase):
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
        self.assertEqual(policy["inlineToolResultBatchBytes"], 24_000)
        report = core.buildProviderProfilesReport(
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
        genuine = userRequestMessage("Fix the current production issue.")
        bridge = {"role": "user", "content": core.CONTINUATION_BRIDGE_MESSAGE}
        partial = syntheticUserMessage(
            "Partial continuation summary for older raw context in this session:\n"
            "Carry-forward requests:\n- stale synthetic objective",
            kind="partial_compaction_summary",
        )
        task = syntheticUserMessage(
            "[PARENT MESSAGE]\nkind: guidance\nmessage_id: msg-1\n\n"
            "synthetic guidance\n[/PARENT MESSAGE]",
            kind="task_notification",
        )
        messages = [genuine, bridge, partial, task]
        self.assertTrue(isGenuineUserRequest(genuine))
        self.assertTrue(
            isGenuineUserRequest(
                userRequestMessage(core.CONTINUATION_BRIDGE_MESSAGE)
            )
        )
        self.assertTrue(
            all(not isGenuineUserRequest(message) for message in messages[1:])
        )
        self.assertEqual(
            _extract_recent_user_requests(messages),
            ["Fix the current production issue."],
        )
        self.assertEqual(core.findLastPlainUserIndex(messages), 0)
        self.assertEqual(
            core.summarizeContinuationRegion(messages)["requests"],
            ["Fix the current production issue."],
        )
        synthesis = core.buildEvidenceBackedCompactionSynthesis(
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
                "latestPlan": None,
                "model": {"provider": "openai", "model": "gpt-5.6-sol"},
            }
        )
        self.assertEqual(
            synthesis["workingContext"]["goals"]["active"],
            ["Fix the current production issue."],
        )

    def test_runtime_compaction_messages_carry_origin(self):
        bridged = core.buildMessagesForWorkingContext(
            {
                "messages": [
                    userRequestMessage("Original objective."),
                    {"role": "assistant", "content": "working"},
                ],
                "metadata": {
                    "compactionState": {"compactedThroughMessageIndex": 1}
                },
            }
        )
        self.assertEqual(
            bridged[0]["metadata"]["agentCoreOrigin"]["kind"],
            "continuation_bridge",
        )
        messages: list[dict[str, Any]] = [
            (
                userRequestMessage(f"request {index}")
                if index % 2 == 0
                else {"role": "assistant", "content": f"finding {index}"}
            )
            for index in range(30)
        ]
        summary = core.applyPartialCompaction(messages)["messages"][0]
        self.assertEqual(
            summary["metadata"]["agentCoreOrigin"]["kind"],
            "partial_compaction_summary",
        )
        self.assertFalse(isGenuineUserRequest(summary))

    def test_origin_copy_isolated_and_no_progress_event_typed(self):
        original = userRequestMessage("hello")
        copied = messageWithOrigin(
            original,
            actor="runtime",
            kind="test",
            synthetic=True,
        )
        self.assertTrue(isGenuineUserRequest(original))
        self.assertFalse(isGenuineUserRequest(copied))
        self.assertNotEqual(original["metadata"], copied["metadata"])
        event = core.createRuntimeEvent(
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
        delegated = messageWithOrigin(
            {"role": "user", "content": "Investigate the child task."},
            actor="subagent",
            kind="subagent_task_prompt",
            intentAuthority="request",
            synthetic=True,
        )
        self.assertTrue(isGenuineUserRequest(delegated))
        self.assertTrue(isPlainUserTurnAnchor(delegated))
        self.assertEqual(
            core.summarizeContinuationRegion([delegated])["requests"],
            ["Investigate the child task."],
        )

    def test_request_fingerprint_ignores_volatile_origin_lineage_only(self):
        first = {
            "model": "gpt-5.6-sol",
            "messages": [
                userRequestMessage(
                    "same request",
                    lineage={"logicalTurnId": "turn-random-a"},
                )
            ],
        }
        second = {
            "model": "gpt-5.6-sol",
            "messages": [
                userRequestMessage(
                    "same request",
                    lineage={"logicalTurnId": "turn-random-b"},
                )
            ],
        }
        self.assertEqual(_request_fingerprint(first), _request_fingerprint(second))
        second["messages"][0]["metadata"]["agentCoreOrigin"]["kind"] = "other"
        self.assertNotEqual(_request_fingerprint(first), _request_fingerprint(second))
