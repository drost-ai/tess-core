from __future__ import annotations

from agent_core.tasks.terminal_transitions import normalizeTaskProcessOutcome
from tests.support.agent_core_case import AgentCoreTestCase


class Package39SignalExitStatusParityTests(AgentCoreTestCase):
    def test_process_outcome_normalizes_posix_signal_encoding(self):
        self.assertEqual(
            normalizeTaskProcessOutcome(-15, None),
            (None, "SIGTERM"),
        )
        self.assertEqual(
            normalizeTaskProcessOutcome(-15, "SIGTERM"),
            (None, "SIGTERM"),
        )

    def test_process_outcome_preserves_numeric_exit(self):
        self.assertEqual(
            normalizeTaskProcessOutcome(7, None),
            (7, None),
        )
