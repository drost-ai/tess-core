from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import agent_core as core
from agent_core.contracts import (
    MonitorRecord,
    SessionRecord,
    TaskMeta,
    TaskRecord,
    validate,
)

from .model import Scenario, ScenarioAssertionError, StepResult
from .normalization import normalize_observations
from .sandbox import ScenarioSandbox


def _assert_contract(records: Any, schema: type, label: str) -> None:
    """Fail the scenario at the capture boundary when a record drifts.

    Non-strict: unknown keys are allowed (fixtures and future writers may add
    fields); missing required keys, wrong types, and out-of-set Literal values
    fail with a precise message instead of a downstream behavioral mismatch.
    """
    items = records if isinstance(records, list) else [records]
    for index, record in enumerate(items):
        if not record:
            continue
        errors = validate(record, schema, strict=False)
        if errors:
            raise ScenarioAssertionError(
                f"{label}[{index}] contract violation: " + "; ".join(errors),
                category="contract",
            )


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
        _assert_contract(observations["session"], SessionRecord, "session")
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
    if "context_sessions" in selected:
        observations["context_sessions"] = state.get("context_sessions") or []
    if "context_driver_results" in selected:
        observations["context_driver_results"] = state.get("context_driver_results") or []
    if "compaction_results" in selected:
        observations["compaction_results"] = state.get("compaction_results") or []
    if "compaction_artifacts" in selected:
        observations["compaction_artifacts"] = state.get("compaction_artifacts") or []
    if "durable_memory" in selected:
        observations["durable_memory"] = state.get("durable_memory") or {}
    if "continuation_results" in selected:
        observations["continuation_results"] = state.get("continuation_results") or []
    if "context_events" in selected:
        observations["context_events"] = state.get("context_events") or []
    if "context_runtime_state" in selected:
        observations["context_runtime_state"] = state.get("context_runtime_state") or {}
    if "context_agent_results" in selected:
        observations["context_agent_results"] = state.get("context_agent_results") or []
    if "remote_driver_results" in selected:
        observations["remote_driver_results"] = state.get("remote_driver_results") or []
    if "remote_sessions" in selected:
        observations["remote_sessions"] = state.get("remote_sessions") or []
    if "bridge_records" in selected:
        observations["bridge_records"] = state.get("bridge_records") or []
    if "bridge_inbox" in selected:
        observations["bridge_inbox"] = state.get("bridge_inbox") or []
    if "bridge_events" in selected:
        observations["bridge_events"] = state.get("bridge_events") or []
    if "bridge_diagnostics" in selected:
        observations["bridge_diagnostics"] = state.get("bridge_diagnostics") or []
    if "remote_events" in selected:
        observations["remote_events"] = state.get("remote_events") or []
    if "collaboration_events" in selected:
        observations["collaboration_events"] = state.get("collaboration_events") or []
    if "collaboration_snapshots" in selected:
        observations["collaboration_snapshots"] = state.get("collaboration_snapshots") or []
    if "remote_runtime_state" in selected:
        observations["remote_runtime_state"] = state.get("remote_runtime_state") or {}
    if "remote_projected_events" in selected:
        observations["remote_projected_events"] = state.get("remote_projected_events") or []
    if "remote_bootstrap_results" in selected:
        observations["remote_bootstrap_results"] = state.get("remote_bootstrap_results") or []
    if "remote_bootstrap_events" in selected:
        observations["remote_bootstrap_events"] = state.get("remote_bootstrap_events") or []
    if "mcp_servers" in selected:
        observations["mcp_servers"] = state.get("mcp_servers") or []
    if "mcp_events" in selected:
        observations["mcp_events"] = stable_mcp_events(state.get("mcp_events") or [])
    if "mcp_status" in selected:
        observations["mcp_status"] = state.get("mcp_status") or []
    if "mcp_tools" in selected:
        observations["mcp_tools"] = state.get("mcp_tools") or []
    if "mcp_resources" in selected:
        observations["mcp_resources"] = state.get("mcp_resources") or []
    if "mcp_results" in selected:
        observations["mcp_results"] = state.get("mcp_results") or []
    if "mcp_plugin_results" in selected:
        observations["mcp_plugin_results"] = state.get("mcp_plugin_results") or []
    if "mcp_driver_results" in selected:
        observations["mcp_driver_results"] = state.get("mcp_driver_results") or []
    if "mcp_package28_results" in selected:
        observations["mcp_package28_results"] = state.get("mcp_package28_results") or []
    if "mcp_package28_events" in selected:
        observations["mcp_package28_events"] = state.get("mcp_package28_events") or []
    if "daemon_state" in selected:
        observations["daemon_state"] = state.get("daemon_state")
    if "daemon_events" in selected:
        observations["daemon_events"] = {
            "live": state.get("daemon_events") or [],
            "persisted": state.get("persisted_daemon_events") or [],
        }
    if "daemon_bootstrap" in selected:
        observations["daemon_bootstrap"] = state.get("daemon_bootstrap") or []
    if "task_records" in selected:
        observations["task_records"] = state.get("task_records") or []
        _assert_contract(observations["task_records"], TaskRecord, "task_records")
    if "task_metadata" in selected:
        observations["task_metadata"] = state.get("task_metadata") or []
        _assert_contract(observations["task_metadata"], TaskMeta, "task_metadata")
    if "task_outputs" in selected:
        observations["task_outputs"] = state.get("task_outputs") or []
    if "task_driver_results" in selected:
        observations["task_driver_results"] = state.get("task_driver_results") or []
    if "health_checks" in selected:
        observations["health_checks"] = state.get("health_checks") or []
    if "monitor_records" in selected:
        observations["monitor_records"] = state.get("monitor_records") or []
        _assert_contract(observations["monitor_records"], MonitorRecord, "monitor_records")
    if "worker_results" in selected:
        observations["worker_results"] = state.get("worker_results") or []
    if "runtime_events" in selected:
        observations["runtime_events"] = state.get("runtime_events") or []
    if "runtime_state" in selected:
        runtime = state.get("runtime")
        observations["runtime_state"] = runtime["getState"]() if runtime and runtime.get("getState") else {}
    if "driver_results" in selected:
        observations["driver_results"] = state.get("driver_results") or []
    if "tool_results" in selected:
        observations["tool_results"] = state.get("tool_results") or []
    if "subprocess_results" in selected:
        observations["subprocess_results"] = state.get("subprocess_results") or []
    if "slash_results" in selected:
        observations["slash_results"] = state.get("slash_results") or []
    if "transcript" in selected and state.get("session"):
        observations["transcript"] = core.buildTranscriptSnapshot(state["session"])
    if "tool_artifacts" in selected and state.get("session"):
        observations["tool_artifacts"] = tree_snapshot(Path(core.getSessionToolArtifactsDir(state["session"]["id"])))
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
    if "provider_fault_matrix" in selected:
        observations["provider_fault_matrix"] = state.get("provider_fault_matrix") or {}
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
        if path.name.endswith(".json.lock") or (path.parent.name == "locks" and path.suffix == ".lock"):
            # Transient advisory-lock sidecars (storage_store.fileLock) are
            # infrastructure, not observable state; keep goldens stable.
            continue
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


def stable_mcp_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda event: (
            str(event.get("event") or ""),
            str((event.get("data") or {}).get("server") or ""),
            str((event.get("data") or {}).get("method") or ""),
            json.dumps(event.get("data") or {}, sort_keys=True),
        ),
    )
