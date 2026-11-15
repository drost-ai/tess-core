from __future__ import annotations

import os
import unittest
from unittest import mock

from tess_core._utils import is_process_alive


class ProcessLivenessTests(unittest.TestCase):
    def test_permission_error_means_alive(self):
        # A pid owned by another user raises PermissionError on signal 0.
        # That process exists; reporting it dead lets reconciliation fail a
        # running task (the historical monitors.py bug).
        with mock.patch.object(os, "kill", side_effect=PermissionError):
            self.assertTrue(is_process_alive(12345))

    def test_missing_process_is_dead(self):
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(is_process_alive(12345))

    def test_invalid_pids_are_dead_without_probing(self):
        with mock.patch.object(os, "kill", side_effect=AssertionError("must not probe")):
            self.assertFalse(is_process_alive(True))
            self.assertFalse(is_process_alive(0))
            self.assertFalse(is_process_alive(-1))
            self.assertFalse(is_process_alive("123"))
            self.assertFalse(is_process_alive(None))

    def test_live_process_is_alive(self):
        self.assertTrue(is_process_alive(os.getpid()))


if __name__ == "__main__":
    unittest.main()
