from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import agent_core as core

from .goldens import compare_golden, expected_observations, render_diff, update_golden
from .loader import load_scenario
from .model import Scenario, ScenarioAssertionError, ScenarioValidationError
from .normalization import normalize_observations
from .parity_model_policy import normalize_compaction_model_policy
from .parity_provenance import inspect_typescript_peer, provenance_error
from .parity_compaction import run_compaction_primitives, run_compaction_run
from .parity_continuation import run_continuation_relief
from .parity_dispatch import run_tool_dispatch_parity
from .paths import DEFAULT_RUNS_DIR, REPO_ROOT


@dataclass
class ParityResult:
    scenarioId: str
    status: str
    runId: str
    runDir: str
    observationsPath: str
    normalizedPath: str
    comparisonPath: str
    golden: dict[str, Any] | None = None
    debugDir: str | None = None
    failure: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "scenarioId": self.scenarioId,
            "status": self.status,
            "runId": self.runId,
            "runDir": self.runDir,
            "observationsPath": self.observationsPath,
            "normalizedPath": self.normalizedPath,
            "comparisonPath": self.comparisonPath,
        }
        if self.golden:
            data["golden"] = self.golden
        if self.debugDir:
            data["debugDir"] = self.debugDir
        if self.failure:
            data["failure"] = self.failure
        return data


def run_parity_scenario(
    scenario: Scenario | str,
    *,
    scenarios_dir: str | None = None,
    runs_dir: str | None = None,
    cleanup: str = "never",
    golden: str = "off",
    goldens_dir: str | None = None,
    typescript_repo: str | None = None,
) -> ParityResult:
    resolved = load_scenario(scenario, scenarios_dir) if isinstance(scenario, str) else scenario
    if "parity" not in resolved.modes:
        raise ScenarioValidationError(f"{resolved.id}: scenario does not declare parity mode")
    paths = _create_paths(resolved.id, runs_dir)
    failure: dict[str, Any] | None = None
    golden_result: dict[str, Any] | None = None
    debug_dir: str | None = None
    try:
        _apply_workspace_setup(paths["python_workspace"], resolved.setup)
        _apply_workspace_setup(paths["typescript_workspace"], resolved.setup)
        actions = [step.input for step in resolved.steps if step.type == "parity_action"]
        python_raw = asyncio.run(_run_python_driver(actions, paths))
        typescript_raw = _run_typescript_driver(
            actions,
            paths,
            typescript_repo,
            require_trusted_reference=golden == "update",
        )
        python_norm = _normalize_side(python_raw, paths, "python")
        typescript_norm = _normalize_side(typescript_raw, paths, "typescript")
        comparison = compare_parity_observations(python_norm, typescript_norm)
        raw, normalized = _build_observations(resolved, paths, python_raw, typescript_raw, python_norm, typescript_norm, comparison)
        raw_path, normalized_path, comparison_path = _write_outputs(paths, raw, normalized, comparison)
        if comparison["status"] != "matched":
            failure = _comparison_failure(comparison)
            debug_dir = _write_parity_debug(paths, failure, python_norm, typescript_norm, comparison)
        elif golden != "off":
            golden_result, failure, debug_dir = _handle_parity_golden(resolved, paths, normalized, golden, goldens_dir)
        status = "failed" if failure else "passed"
        return ParityResult(resolved.id, status, paths["run_id"], str(paths["run_dir"]), raw_path, normalized_path, comparison_path, golden_result, debug_dir, failure)
    finally:
        core.resetConfiguredRuntimeHome()
        _restore_env(paths["previous_env"])
        if cleanup == "success" and failure is None:
            shutil.rmtree(paths["run_dir"], ignore_errors=True)


def compare_parity_observations(python: dict[str, Any], typescript: dict[str, Any]) -> dict[str, Any]:
    families: list[dict[str, Any]] = []
    py_cases = {case.get("name"): case for case in python.get("cases") or []}
    ts_cases = {case.get("name"): case for case in typescript.get("cases") or []}
    if python.get("status") != "passed":
        return _error_comparison("python_driver_failed", python.get("error"))
    if typescript.get("status") != "passed":
        return _error_comparison("typescript_driver_failed", typescript.get("error"))
    for name in sorted(set(py_cases) | set(ts_cases)):
        py_case = py_cases.get(name)
        ts_case = ts_cases.get(name)
        if not py_case or not ts_case:
            families.append({"name": str(name), "status": "missing", "classification": "scenario_assumption_invalid"})
            continue
        for family in _families_for(py_case, ts_case):
            families.append(_compare_family(str(name), family, py_case.get(family), ts_case.get(family)))
    mismatches = [family for family in families if family["status"] != "matched"]
    return {
        "status": "mismatched" if mismatches else "matched",
        "classification": mismatches[0]["classification"] if mismatches else "matched",
        "families": families,
    }


async def _run_python_driver(actions: list[dict[str, Any]], paths: dict[str, Any]) -> dict[str, Any]:
    previous_home = os.environ.get("AGENT_CORE_HOME")
    paths["previous_env"]["AGENT_CORE_HOME"] = previous_home
    os.environ["AGENT_CORE_HOME"] = str(paths["python_home"])
    core.configureRuntimeHome(str(paths["python_home"]))
    observations: dict[str, Any] = {"implementation": "python", "status": "passed", "cases": []}
    try:
        for action in actions:
            observations["cases"].append(await _run_python_action(action, paths))
    except Exception as exc:
        observations["status"] = "failed"
        observations["error"] = {"message": str(exc), "type": type(exc).__name__}
    return _clean(observations)


async def _run_python_action(action: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    data = {**action, **(action.get("python") or {})}
    if data.get("action") == "compaction_primitives":
        return await run_compaction_primitives(data, paths)
    if data.get("action") == "compaction_run":
        return await run_compaction_run(data, paths)
    if data.get("action") == "continuation_relief":
        return await run_continuation_relief(data, paths)
    if data.get("action") == "tool_dispatch":
        return await run_tool_dispatch_parity(data)
    if data.get("action") != "runtime_state_smoke":
        raise ScenarioAssertionError(f"unsupported parity action: {data.get('action')}", category="harness")
    config = await core.resolveConfig(
        {
            "provider": data.get("provider", "openai"),
            "model": data.get("model", "gpt-5.4"),
            "permissionMode": data.get("permissionMode", "bypassPermissions"),
            "runtimeHome": str(paths["python_home"]),
        },
        str(paths["python_workspace"]),
    )
    session = core.createEmptySession(
        {
            "cwd": str(paths["python_workspace"]),
            "provider": config["provider"],
            "providerProfileName": config.get("providerProfileName"),
            "model": config["model"],
            "effort": config.get("effort"),
            "systemPrompt": config["systemPrompt"],
            "permissionMode": config["permissionMode"],
            "sessionId": data.get("sessionId") or "parity-session",
        }
    )
    session["createdAt"] = data.get("createdAt") or "2026-01-01T00:00:00.000Z"
    session["updatedAt"] = data.get("updatedAt") or "2026-01-01T00:00:01.000Z"
    session.setdefault("metadata", {})["workspaceName"] = data.get("workspaceName") or "Parity Workspace"
    await core.saveSession(session)
    loaded = await core.loadSession(session["id"])
    events = _python_runtime_events(session, data)
    runtime_state = core.createInitialRuntimeState(session)
    for event in events:
        runtime_state = core.reduceRuntimeState(runtime_state, event)
    return _clean(
        {
            "name": data.get("name") or data.get("action"),
            "capabilities": {
                "defaultProvider": core.getDefaultProvider(),
                "defaultModel": core.getDefaultModel(),
                "providers": list(core.listProviders()),
                "effortLevels": list(core.listEffortLevels()),
            },
            "config": _project_config(config),
            "session": _project_session(session),
            "storage": {"loadedSessionId": loaded.get("id") if loaded else None, "listedSessionCount": len(await core.listSessions())},
            "runtimeEvents": [_project_runtime_event(event) for event in events],
            "runtimeState": _project_runtime_state(runtime_state),
        }
    )


def _run_typescript_driver(
    actions: list[dict[str, Any]],
    paths: dict[str, Any],
    typescript_repo: str | None,
    *,
    require_trusted_reference: bool = False,
) -> dict[str, Any]:
    repo = typescript_repo or os.environ.get("AGENT_CORE_TYPESCRIPT_REPO") or "/Users/dev/agents/agent-core"
    provenance = inspect_typescript_peer(repo)
    invalid = provenance_error(
        provenance,
        require_trusted_reference=require_trusted_reference,
    )
    if invalid:
        return {
            "implementation": "typescript",
            "status": "failed",
            "provenance": provenance,
            "error": {"message": invalid, "type": "ParityReferenceError"},
        }
    input_path = paths["raw"] / "typescript-input.json"
    output_path = paths["raw"] / "typescript-driver-output.json"
    payload = {
        "actions": actions,
        "runtimeHome": str(paths["typescript_home"]),
        "workspace": str(paths["typescript_workspace"]),
        "typescriptRepo": repo,
        "outputPath": str(output_path),
    }
    input_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    completed = subprocess.run(["node", str(REPO_ROOT / "agent_core_harness" / "parity_ts_driver.mjs"), str(input_path)], text=True, capture_output=True, timeout=30)
    if completed.returncode != 0:
        return {"implementation": "typescript", "status": "failed", "provenance": provenance, "error": {"message": completed.stderr.strip() or completed.stdout.strip(), "returnCode": completed.returncode}}
    result = json.loads(output_path.read_text(encoding="utf-8"))
    result["provenance"] = provenance
    return result


def _python_runtime_events(session: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    prompt = data.get("prompt") or "hello parity"
    assistant_text = data.get("assistantText") or "hello from runtime"
    return [
        core.createRuntimeEvent({"event": "session.activated", "timestamp": "2026-01-01T00:00:02.000Z", "data": {"session_id": session["id"], "cwd": session["cwd"], "provider": session["provider"], "model": session["model"], "permission_mode": session["permissionMode"], "workspace_name": session["metadata"].get("workspaceName")}}),
        core.createRuntimeEvent({"event": "turn.started", "timestamp": "2026-01-01T00:00:03.000Z", "data": {"session_id": session["id"], "prompt": prompt, "model": session["model"]}}),
        core.createRuntimeEvent({"event": "message.user", "timestamp": "2026-01-01T00:00:04.000Z", "data": {"session_id": session["id"], "text": prompt}}),
        core.createRuntimeEvent({"event": "message.assistant", "timestamp": "2026-01-01T00:00:05.000Z", "data": {"session_id": session["id"], "text": assistant_text, "model": session["model"]}}),
        core.createRuntimeEvent({"event": "turn.completed", "timestamp": "2026-01-01T00:00:06.000Z", "data": {"session_id": session["id"], "stop_reason": data.get("stopReason") or "stop", "message": assistant_text, "model": session["model"]}}),
    ]


def _project_config(config: dict[str, Any]) -> dict[str, Any]:
    return _clean({key: config.get(key) for key in ("provider", "model", "permissionMode", "effort", "providerProfileName")})


def _project_session(session: dict[str, Any]) -> dict[str, Any]:
    metadata = session.get("metadata") or {}
    return _clean(
        {
            "id": session.get("id"),
            "cwd": session.get("cwd"),
            "provider": session.get("provider"),
            "model": session.get("model"),
            "permissionMode": session.get("permissionMode"),
            "metadata": {key: metadata.get(key) for key in ("workspaceName", "originalCwd", "usage", "compactions", "depth", "childSessionIds", "childAgentIds", "planMode")},
        }
    )


def _project_runtime_event(event: dict[str, Any]) -> dict[str, Any]:
    return _clean({"kind": event.get("kind"), "sessionId": event.get("sessionId"), "payload": event.get("payload"), "legacyEvent": event.get("legacyEvent")})


def _project_runtime_state(state: dict[str, Any]) -> dict[str, Any]:
    return _clean(
        {
            "session": {"activeSessionId": state["session"].get("activeSessionId"), "summary": _pick(state["session"].get("summary"), ("id", "provider", "model", "permissionMode", "workspaceName", "cwd")), "lastActivatedAt": state["session"].get("lastActivatedAt")},
            "turn": {"status": state["turn"].get("status"), "model": state["turn"].get("model"), "startedAt": state["turn"].get("startedAt"), "lastOutcome": _pick(state["turn"].get("lastOutcome"), ("kind", "message", "stopReason", "model", "updatedAt"))},
            "provider": {"phase": state["provider"].get("phase")},
            "compaction": {"status": state["compaction"].get("status")},
            "daemon": {"phase": state["daemon"].get("phase")},
            "tasks": {key: state["tasks"].get(key) for key in ("total", "activeCount", "backgroundedCount", "awaitingInputCount", "failedCount", "labels")},
            "bridge": {"attached": state["bridge"].get("attached")},
            "collaboration": {key: state["collaboration"].get(key) for key in ("activeCount", "awaitingInputCount", "backgroundCount", "remoteTitle", "labels")},
        }
    )


def _compare_family(case: str, family: str, python: Any, typescript: Any) -> dict[str, Any]:
    if python == typescript:
        return {"name": f"{case}.{family}", "status": "matched", "classification": "matched"}
    return {"name": f"{case}.{family}", "status": "mismatched", "classification": "behavior_drift", "diff": render_diff(typescript, python, fromfile="typescript", tofile="python")}


def _families_for(python: dict[str, Any], typescript: dict[str, Any]) -> list[str]:
    preferred = [
        "capabilities",
        "config",
        "session",
        "storage",
        "runtimeEvents",
        "runtimeState",
        "modelPlan",
        "modelCandidates",
        "tokenEstimates",
        "autoCompact",
        "workingMessages",
        "systemPromptContext",
        "outcome",
        "attempts",
        "finalOutcome",
        "retryLineage",
        "repair",
        "skips",
        "models",
        "verification",
        "artifacts",
        "workingContext",
        "checkpoint",
        "sessionMetadata",
        "modelCalls",
        "pressure",
        "selection",
        "steps",
        "messages",
        "reinjection",
    ]
    available = set(python) | set(typescript)
    families = [family for family in preferred if family in available]
    return families + sorted(available - set(families) - {"name"})


def _error_comparison(classification: str, error: Any) -> dict[str, Any]:
    return {"status": "mismatched", "classification": classification, "families": [{"name": classification, "status": "errored", "classification": classification, "error": error}]}


def _build_observations(scenario: Scenario, paths: dict[str, Any], python_raw: dict[str, Any], typescript_raw: dict[str, Any], python_norm: dict[str, Any], typescript_norm: dict[str, Any], comparison: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    run = {"id": paths["run_id"], "runDir": str(paths["run_dir"]), "raw": str(paths["raw"]), "normalized": str(paths["normalized"]), "debug": str(paths["debug"])}
    raw = {"scenario": _scenario_summary(scenario), "run": run, "parity": {"status": comparison["status"]}, "python": python_raw, "typescript": typescript_raw, "comparison": comparison}
    normalized = {"scenario": _scenario_summary(scenario), "run": normalize_observations(run, _view(paths, "root")), "parity": {"status": comparison["status"]}, "python": python_norm, "typescript": typescript_norm, "comparison": comparison}
    return raw, normalized


def _write_outputs(paths: dict[str, Any], raw: dict[str, Any], normalized: dict[str, Any], comparison: dict[str, Any]) -> tuple[str, str, str]:
    raw_path = paths["raw"] / "observations.json"
    normalized_path = paths["normalized"] / "observations.json"
    comparison_path = paths["normalized"] / "comparison.json"
    raw_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    normalized_path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    comparison_path.write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (paths["normalized"] / "python.observations.json").write_text(json.dumps(normalized["python"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (paths["normalized"] / "typescript.observations.json").write_text(json.dumps(normalized["typescript"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(raw_path), str(normalized_path), str(comparison_path)


def _handle_parity_golden(scenario: Scenario, paths: dict[str, Any], normalized: dict[str, Any], mode: str, goldens_dir: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    if mode == "update":
        return update_golden(scenario, normalized, goldens_dir), None, None
    result = compare_golden(scenario, normalized, goldens_dir=goldens_dir, diff_path=paths["debug"] / "golden.diff")
    if result.get("status") == "matched":
        return result, None, None
    expected = expected_observations(scenario, goldens_dir)
    failure = {"category": "golden", "message": result.get("message", "Golden comparison failed."), "classification": "harness_normalization_gap"}
    if expected is not None:
        (paths["debug"] / "expected-observations.normalized.json").write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (paths["debug"] / "failure.json").write_text(json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result, failure, str(paths["debug"])


def _write_parity_debug(paths: dict[str, Any], failure: dict[str, Any], python: dict[str, Any], typescript: dict[str, Any], comparison: dict[str, Any]) -> str:
    paths["debug"].mkdir(parents=True, exist_ok=True)
    files = {"failure.json": failure, "python.normalized.json": python, "typescript.normalized.json": typescript, "comparison.json": comparison}
    for name, value in files.items():
        (paths["debug"] / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    diffs = [family.get("diff", "") for family in comparison.get("families") or [] if family.get("diff")]
    (paths["debug"] / "parity.diff").write_text("\n".join(diffs), encoding="utf-8")
    return str(paths["debug"])


def _comparison_failure(comparison: dict[str, Any]) -> dict[str, Any]:
    classification = comparison.get("classification")
    message = "Python and TypeScript normalized observations differ."
    if classification in {"python_driver_failed", "typescript_driver_failed"}:
        message = f"Parity driver failed: {classification}."
    return {"category": "parity", "message": message, "classification": classification, "families": [family for family in comparison.get("families", []) if family.get("status") != "matched"]}


def _normalize_side(value: dict[str, Any], paths: dict[str, Any], side: str) -> dict[str, Any]:
    comparable = {key: item for key, item in value.items() if key != "provenance"}
    normalized = normalize_observations(comparable, _view(paths, side))
    return normalize_compaction_model_policy(normalized)


def _view(paths: dict[str, Any], side: str) -> SimpleNamespace:
    prefix = f"{side}_" if side in {"python", "typescript"} else ""
    return SimpleNamespace(run_id=paths["run_id"], run_dir=paths["run_dir"], home=paths.get(f"{prefix}home", paths["run_dir"]), workspace=paths.get(f"{prefix}workspace", paths["run_dir"]))


def _create_paths(scenario_id: str, runs_dir: str | None) -> dict[str, Any]:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = (Path(runs_dir) if runs_dir else DEFAULT_RUNS_DIR) / run_id / scenario_id
    paths = {"run_id": run_id, "run_dir": run_dir, "previous_env": {}, "raw": run_dir / "raw", "normalized": run_dir / "normalized", "debug": run_dir / "debug"}
    for side in ("python", "typescript"):
        paths[f"{side}_home"] = run_dir / side / "home"
        paths[f"{side}_workspace"] = run_dir / side / "workspace"
    for path in (paths["raw"], paths["normalized"], paths["debug"], paths["python_home"], paths["python_workspace"], paths["typescript_home"], paths["typescript_workspace"]):
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _apply_workspace_setup(workspace: Path, setup: dict[str, Any]) -> None:
    workspace_setup = setup.get("workspace") or {}
    for rel_path in workspace_setup.get("directories") or []:
        (workspace / rel_path).mkdir(parents=True, exist_ok=True)
    for rel_path, content in (workspace_setup.get("files") or {}).items():
        target = workspace / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _scenario_summary(scenario: Scenario) -> dict[str, str]:
    return {"id": scenario.id, "title": scenario.title, "area": scenario.area}


def _pick(value: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in keys if value and value.get(key) is not None}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _restore_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
