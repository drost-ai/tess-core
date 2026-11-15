from __future__ import annotations

from tess_core.tasks.terminal_transitions import normalize_task_process_outcome
from tests.support.tess_core_case import TessCoreTestCase


class Package39SignalExitStatusParityTests(TessCoreTestCase):
    def test_process_outcome_normalizes_posix_signal_encoding(self):
        self.assertEqual(
            normalize_task_process_outcome(-15, None),
            (None, "SIGTERM"),
        )
        self.assertEqual(
            normalize_task_process_outcome(-15, "SIGTERM"),
            (None, "SIGTERM"),
        )

    def test_process_outcome_preserves_numeric_exit(self):
        self.assertEqual(
            normalize_task_process_outcome(7, None),
            (7, None),
        )
