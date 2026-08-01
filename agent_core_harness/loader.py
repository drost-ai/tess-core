from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from .model import Scenario, ScenarioStep, ScenarioValidationError
from .paths import DEFAULT_SCENARIOS_DIR


SUPPORTED_MODES = {"python", "parity"}
SUPPORTED_STEPS = {
    "assert_file_exists",
    "assert_observation",
    "assert_provider_capture",
    "assert_provider_response",
    "assert_driver_result",
    "assert_bootstrap",
    "assert_compaction_artifact",
    "assert_context_result",
    "assert_context_runtime_state",
    "assert_daemon_event",
    "assert_bridge_diagnostics",
    "assert_collaboration_snapshot",
    "assert_mcp_bootstrap",
    "assert_mcp_event",
    "assert_mcp_result",
    "assert_mcp_status",
    "assert_remote_result",
    "assert_remote_runtime_state",
    "assert_runtime_event",
    "assert_task_output",
    "assert_task_record",
    "bootstrap_mcp_plugin_runtime",
    "assert_session",
    "apply_context_relief",
    "apply_mcp_catalog_change",
    "bootstrap_runtime",
    "bootstrap_remote_runtime",
    "call_tool",
    "call_mcp_generated_tool",
    "call_mcp_tool",
    "capture",
    "close_mcp_runtime",
    "create_session",
    "create_remote_bridge",
    "create_monitor",
    "create_mcp_plugin_fixture",
    "create_mcp_runtime",
    "daemon_subscribe",
    "ensure_daemon",
    "get_daemon_bootstrap",
    "collect_bridge_diagnostics",
    "list_mcp_resources",
    "list_mcp_tools",
    "load_session",
    "parity_action",
    "provider_request",
    "read_mcp_resource",
    "record_mcp_protocol_probe",
    "recover_mcp_headless_auth",
    "read_task_output",
    "restart_daemon",
    "reconnect_bridge",
    "poll_bridge_inbox",
    "project_remote_events",
    "queue_bridge_message",
    "refresh_collaboration",
    "run_context_compaction",
    "run_context_fallback",
    "run_python",
    "run_agent",
    "run_mcp_dispatch_probe",
    "run_mcp_plan_probe",
    "run_slash_command",
    "run_subprocess",
    "run_worker_entrypoint",
    "save_session",
    "seed_recovery_task",
    "seed_collaboration_fixture",
    "seed_context_session",
    "set_bridge_emit_failure",
    "project_context_events",
    "run_reactive_context_retry",
    "run_anthropic_fault_matrix",
    "run_reactive_context_cancellation",
    "start_fake_mcp",
    "start_background_task",
    "start_health_check",
    "close_bridge_runtime",
    "update_child_agent_status",
    "stop_task",
    "wait_health_check",
    "wait_task_status",
}
SUPPORTED_CAPTURE = {
    "daemon_bootstrap",
    "daemon_events",
    "daemon_state",
    "bridge_diagnostics",
    "bridge_events",
    "bridge_inbox",
    "bridge_records",
    "collaboration_events",
    "collaboration_snapshots",
    "compaction_artifacts",
    "compaction_results",
    "context_agent_results",
    "context_driver_results",
    "context_events",
    "context_runtime_state",
    "context_sessions",
    "continuation_results",
    "durable_memory",
    "environment",
    "logs",
    "mcp_driver_results",
    "mcp_events",
    "mcp_plugin_results",
    "mcp_package28_events",
    "mcp_package28_results",
    "mcp_resources",
    "mcp_results",
    "mcp_servers",
    "mcp_status",
    "mcp_tools",
    "parity_comparison",
    "parity_python",
    "parity_summary",
    "parity_typescript",
    "provider_events",
    "provider_requests",
    "provider_response",
    "provider_state",
    "provider_fault_matrix",
    "remote_bootstrap_events",
    "remote_bootstrap_results",
    "remote_driver_results",
    "remote_events",
    "remote_projected_events",
    "remote_runtime_state",
    "remote_sessions",
    "driver_results",
    "runtime_events",
    "runtime_state",
    "session",
    "slash_results",
    "subprocess_results",
    "step_results",
    "storage_tree",
    "task_records",
    "task_metadata",
    "task_outputs",
    "task_driver_results",
    "health_checks",
    "monitor_records",
    "tool_artifacts",
    "tool_results",
    "transcript",
    "workspace_tree",
    "worker_results",
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
    tags = _optional_string_list(data, "tags")
    capabilities = _optional_string_list(data, "capabilities")
    ci = data.get("ci") or {}
    if not isinstance(ci, dict):
        raise ScenarioValidationError(f"{scenario_id}: ci must be an object")
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
    return Scenario(scenario_id, title, area, modes, setup, steps, capture, path, tags, capabilities, ci)


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


def _optional_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key) or []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ScenarioValidationError(f"Scenario field {key!r} must be a string list")
    return [item.strip() for item in value]
