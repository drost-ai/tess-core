from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIOS_DIR = REPO_ROOT / "tests" / "scenarios"
DEFAULT_RUNS_DIR = REPO_ROOT / ".scenario-runs"
