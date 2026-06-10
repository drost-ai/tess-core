from __future__ import annotations

import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mypy_available() -> bool:
    try:
        import mypy  # noqa: F401
    except Exception:
        return False
    return True


class ContractsTypecheckTests(unittest.TestCase):
    """Blocking type-check lane over the typed contract boundaries.

    mypy is a dev/CI tool, not a runtime dependency; the lane skips when it is
    not installed so the suite stays runnable on a bare interpreter.
    """

    @unittest.skipUnless(_mypy_available(), "mypy is not installed")
    def test_contracts_lane_is_clean(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--config-file", "pyproject.toml"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"mypy reported errors:\n{result.stdout}\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
