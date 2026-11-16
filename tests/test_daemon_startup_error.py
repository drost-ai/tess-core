from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tess_core.daemon.lifecycle import _last_bootstrap_line


class DaemonStartupErrorTests(unittest.TestCase):
    """A daemon that dies at start (a bad setting, most often) writes why to its
    bootstrap log; the host's error must carry that line, not only the path."""

    def test_reports_the_last_line_the_daemon_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "bootstrap.log"
            self.assertIsNone(_last_bootstrap_line(str(log)))
            log.write_text("", encoding="utf-8")
            self.assertIsNone(_last_bootstrap_line(str(log)))
            log.write_text("Traceback (most recent call last):\n  File x\nValueError: Unknown knob(s) in settings: subagents.max_livee.\n\n", encoding="utf-8")
            self.assertEqual(_last_bootstrap_line(str(log)), "ValueError: Unknown knob(s) in settings: subagents.max_livee.")
            log.write_text("ValueError: " + "x" * 2_000 + "\n", encoding="utf-8")
            self.assertEqual(len(_last_bootstrap_line(str(log)) or ""), 300)


if __name__ == "__main__":
    unittest.main()
