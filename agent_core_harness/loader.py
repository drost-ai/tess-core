from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from .model import Scenario, ScenarioStep, ScenarioValidationError
from .paths import DEFAULT_SCENARIOS_DIR


SUPPORTED_MODES = {"python"}
SUPPORTED_STEPS = {
    "assert_file_exists",
    "assert_observation",
    "assert_provider_capture",
    "assert_provider_response",
    "assert_driver_result",
    "assert_runtime_event",
    "assert_session",
    "bootstrap_runtime",
    "call_tool",
    "capture",
    "create_session",
    "load_session",
    "provider_request",
    "run_python",
    "run_agent",
    "run_slash_command",
    "run_subprocess",
    "save_session",
}
SUPPORTED_CAPTURE = {
    "daemon_state",
    "environment",
    "logs",
    "provider_events",
    "provider_requests",
    "provider_response",
    "provider_state",
    "driver_results",
    "runtime_events",
    "runtime_state",
    "session",
    "slash_results",
    "subprocess_results",
    "step_results",
    "storage_tree",
    "task_records",
    "tool_artifacts",
    "tool_results",
    "transcript",
    "workspace_tree",
}


def load_scenarios(scenarios_dir: str | None = None) -> list[Scenario]:
    root = DEFAULT_SCENARIOS_DIR if scenarios_dir is None else DEFAULT_SCENARIOS_DIR.__class__(scenarios_dir)
    scenarios: list[Scenario] = []
    seen: dict[str, str] = {}
    for path in sorted(root.rglob("*.json")):
        scenario = _load_path(path)
        if scenario.id in seen:
            raise ScenarioValidationError(f"Duplicate scenario id {scenario.id!r}: {seen[scenario.id]} and {path}")
        seen[scenario.id] = str(path)
        scenarios.append(scenario)
    return scenarios


def load_scenario(scenario_id: str, scenarios_dir: str | None = None) -> Scenario:
    for scenario in load_scenarios(scenarios_dir):
        if scenario.id == scenario_id:
            return scenario
    raise ScenarioValidationError(f"Scenario not found: {scenario_id}")


def _load_path(path: Any) -> Scenario:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ScenarioValidationError(f"Scenario manifest must be an object: {path}")
    return _parse_scenario(data, path)


def _parse_scenario(data: dict[str, Any], path: Any = None) -> Scenario:
    scenario_id = _require_string(data, "id")
    title = _require_string(data, "title")
    area = _require_string(data, "area")
    modes = _require_string_list(data, "mode")
    unsupported_modes = sorted(set(modes) - SUPPORTED_MODES)
    if unsupported_modes:
        raise ScenarioValidationError(f"{scenario_id}: unsupported mode(s): {', '.join(unsupported_modes)}")
    setup = data.get("setup") or {}
    if not isinstance(setup, dict):
        raise ScenarioValidationError(f"{scenario_id}: setup must be an object")
    _validate_workspace_setup(scenario_id, setup.get("workspace") or {})
    capture = data.get("capture") or {}
    if not isinstance(capture, dict):
        raise ScenarioValidationError(f"{scenario_id}: capture must be an object")
    include = capture.get("include") or []
    if not isinstance(include, list) or any(not isinstance(item, str) for item in include):
        raise ScenarioValidationError(f"{scenario_id}: capture.include must be a string list")
    unsupported_capture = sorted(set(include) - SUPPORTED_CAPTURE)
    if unsupported_capture:
        raise ScenarioValidationError(f"{scenario_id}: unsupported capture family: {', '.join(unsupported_capture)}")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScenarioValidationError(f"{scenario_id}: steps must be a non-empty list")
    steps = [_parse_step(scenario_id, index, item) for index, item in enumerate(raw_steps)]
    return Scenario(scenario_id, title, area, modes, setup, steps, capture, path)


def _parse_step(scenario_id: str, index: int, item: Any) -> ScenarioStep:
    if not isinstance(item, dict) or len(item) != 1:
        raise ScenarioValidationError(f"{scenario_id}: step {index} must have exactly one step type")
    step_type, value = next(iter(item.items()))
    if step_type not in SUPPORTED_STEPS:
        raise ScenarioValidationError(f"{scenario_id}: unknown step type {step_type!r}")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ScenarioValidationError(f"{scenario_id}: step {index} input must be an object")
    return ScenarioStep(step_type, value)


def _validate_workspace_setup(scenario_id: str, workspace: Any) -> None:
    if not isinstance(workspace, dict):
        raise ScenarioValidationError(f"{scenario_id}: setup.workspace must be an object")
    files = workspace.get("files") or {}
    directories = workspace.get("directories") or []
    if not isinstance(files, dict):
        raise ScenarioValidationError(f"{scenario_id}: setup.workspace.files must be an object")
    if not isinstance(directories, list):
        raise ScenarioValidationError(f"{scenario_id}: setup.workspace.directories must be a list")
    for rel_path, content in files.items():
        _validate_relative_path(scenario_id, rel_path)
        if not isinstance(content, str):
            raise ScenarioValidationError(f"{scenario_id}: workspace file {rel_path!r} content must be a string")
    for rel_path in directories:
        _validate_relative_path(scenario_id, rel_path)


def _validate_relative_path(scenario_id: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioValidationError(f"{scenario_id}: workspace path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise ScenarioValidationError(f"{scenario_id}: workspace path escapes the sandbox: {value!r}")


def _require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScenarioValidationError(f"Scenario field {key!r} must be a non-empty string")
    return value.strip()


def _require_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ScenarioValidationError(f"Scenario field {key!r} must be a non-empty string list")
    return [item.strip() for item in value]
