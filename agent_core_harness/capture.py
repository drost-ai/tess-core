from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .model import Scenario, StepResult
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


def normalize_observations(value: Any, sandbox: ScenarioSandbox) -> Any:
    uuid_map: dict[str, str] = {}

    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: normalize(val) for key, val in item.items()}
        if isinstance(item, list):
            return [normalize(val) for val in item]
        if isinstance(item, str):
            return _normalize_string(item, sandbox, uuid_map)
        return item

    return normalize(value)


def _normalize_string(value: str, sandbox: ScenarioSandbox, uuid_map: dict[str, str]) -> str:
    normalized = value
    replacements = [
        (str(sandbox.run_dir), "<run-dir>"),
        (str(sandbox.home), "<runtime-home>"),
        (str(sandbox.workspace), "<workspace>"),
    ]
    for raw, marker in replacements:
        normalized = normalized.replace(raw, marker)
    if re.match(r"^\d{4}-\d{2}-\d{2}T", normalized):
        return "<timestamp>"
    uuid_pattern = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)

    def replace_uuid(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw not in uuid_map:
            uuid_map[raw] = f"<uuid:{len(uuid_map) + 1}>"
        return uuid_map[raw]

    return uuid_pattern.sub(replace_uuid, normalized)
