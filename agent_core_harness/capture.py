from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .model import Scenario, StepResult
from .normalization import normalize_observations
from .sandbox import ScenarioSandbox


def collect_observations(
    scenario: Scenario,
    sandbox: ScenarioSandbox,
    state: dict[str, Any],
    steps: list[StepResult],
    include: list[str],
) -> dict[str, Any]:
    observations: dict[str, Any] = {
        "scenario": {"id": scenario.id, "title": scenario.title, "area": scenario.area},
        "run": {"id": sandbox.run_id, **sandbox.paths_dict()},
    }
    selected = set(include or [])
    if "session" in selected and state.get("session"):
        observations["session"] = state["session"]
    if "storage_tree" in selected:
        observations["storage_tree"] = tree_snapshot(sandbox.home)
    if "workspace_tree" in selected:
        observations["workspace_tree"] = tree_snapshot(sandbox.workspace)
    if "step_results" in selected:
        observations["step_results"] = [step.to_dict() for step in steps]
    if "environment" in selected:
        observations["environment"] = {
            "AGENT_CORE_HOME": os.environ.get("AGENT_CORE_HOME"),
            "AGENT_CORE_HARNESS_RUN_ID": os.environ.get("AGENT_CORE_HARNESS_RUN_ID"),
            "AGENT_CORE_HARNESS_SCENARIO_ID": os.environ.get("AGENT_CORE_HARNESS_SCENARIO_ID"),
        }
    if "logs" in selected:
        observations["logs"] = tree_snapshot(sandbox.logs)
    provider_runtime = state.get("provider_runtime")
    if provider_runtime:
        provider_observations = provider_runtime.observations()
        if "provider_requests" in selected:
            observations["provider_requests"] = provider_observations["provider_requests"]
        if "provider_events" in selected:
            observations["provider_events"] = provider_observations["provider_events"]
        if "provider_response" in selected:
            observations["provider_response"] = provider_observations["provider_results"]
        if "provider_state" in selected:
            observations["provider_state"] = provider_observations["provider_state"]
    return observations


def write_observations(sandbox: ScenarioSandbox, observations: dict[str, Any]) -> tuple[str, str]:
    raw_path = sandbox.raw / "observations.json"
    normalized_path = sandbox.normalized / "observations.json"
    raw_path.write_text(json.dumps(observations, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    normalized = normalize_observations(observations, sandbox)
    normalized_path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(raw_path), str(normalized_path)


def tree_snapshot(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append({"path": rel, "type": "dir"})
            continue
        item: dict[str, Any] = {"path": rel, "type": "file", "size": path.stat().st_size}
        if path.suffix == ".json" and path.stat().st_size < 20000:
            try:
                item["json"] = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        entries.append(item)
    return entries
