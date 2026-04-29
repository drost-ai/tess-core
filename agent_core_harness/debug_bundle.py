from __future__ import annotations

import json
import stat
from typing import Any

from .capture import normalize_observations
from .model import Scenario
from .sandbox import ScenarioSandbox


def write_debug_bundle(
    scenario: Scenario,
    sandbox: ScenarioSandbox,
    observations: dict[str, Any],
    failure: dict[str, Any],
) -> str:
    sandbox.debug.mkdir(parents=True, exist_ok=True)
    manifest_path = sandbox.debug / "manifest.json"
    raw_path = sandbox.debug / "raw-observations.json"
    normalized_path = sandbox.debug / "normalized-observations.json"
    failure_path = sandbox.debug / "failure.json"
    replay_path = sandbox.debug / "replay.sh"
    manifest = json.loads(scenario.path.read_text(encoding="utf-8")) if scenario.path else {}
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    raw_path.write_text(json.dumps(observations, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    normalized = normalize_observations(observations, sandbox)
    normalized_path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failure_path.write_text(json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    replay = f"#!/usr/bin/env bash\nPYTHONDONTWRITEBYTECODE=1 python3 -m agent_core_harness run {scenario.id}\n"
    replay_path.write_text(replay, encoding="utf-8")
    replay_path.chmod(replay_path.stat().st_mode | stat.S_IXUSR)
    return str(sandbox.debug)
