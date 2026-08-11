from __future__ import annotations

from tests.support.agent_core_case import *


class CompactionDecisionEventTests(AgentCoreTestCase):
    def test_complete_request_measurement_survives_typed_projection(self):
        event = core.create_runtime_event(
            {
                "event": "compaction.decision",
                "createdAt": "t-0d",
                "data": {
                    "should_checkpoint": False,
                    "reason": "complete_request_below_threshold",
                    "measurement_scope": "provider_request_complete",
                    "current_token_estimate": 42_000,
                    "current_token_source": "codex_estimate",
                    "compaction_threshold_tokens": 200_000,
                    "max_context_length": 272_000,
                },
            }
        )

        self.assertEqual(event["payload"]["measurementScope"], "provider_request_complete")
        self.assertEqual(event["payload"]["currentTokenEstimate"], 42_000)
        self.assertEqual(event["payload"]["compactionThresholdTokens"], 200_000)
        state = core.reduce_runtime_state(core.create_initial_runtime_state(), event)
        self.assertEqual(state["compaction"]["lastDecision"]["currentTokenSource"], "codex_estimate")
        self.assertEqual(state["compaction"]["lastDecision"]["maxContextLength"], 272_000)
