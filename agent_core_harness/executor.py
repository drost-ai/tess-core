from __future__ import annotations

import asyncio
from typing import Any

import agent_core as core

from .assertions import assert_file_exists, assert_observation, assert_session, validate_workspace_rel_path
from .capture import collect_observations, write_observations
from .debug_bundle import write_debug_bundle
from .loader import load_scenario
from .model import Scenario, ScenarioAssertionError, ScenarioResult, StepResult
from .sandbox import ScenarioSandbox


async def run_scenario_async(
    scenario: Scenario | str,
    *,
    scenarios_dir: str | None = None,
    runs_dir: str | None = None,
    cleanup: str = "never",
) -> ScenarioResult:
    resolved = load_scenario(scenario, scenarios_dir) if isinstance(scenario, str) else scenario
    sandbox = ScenarioSandbox(resolved.id, cleanup=cleanup, **({"runs_dir": runs_dir} if runs_dir else {})).create()
    steps: list[StepResult] = []
    state: dict[str, Any] = {}
    observations: dict[str, Any] = {}
    failure: dict[str, Any] | None = None
    raw_path = ""
    normalized_path = ""
    try:
        sandbox.apply_setup(resolved.setup)
        for index, step in enumerate(resolved.steps):
            started = core.now_iso()
            try:
                observation_keys = await _execute_step(step.type, step.input, resolved, sandbox, state, steps, observations)
                steps.append(StepResult(index, step.type, "passed", started, core.now_iso(), observation_keys))
            except ScenarioAssertionError as exc:
                failure = {"category": exc.category, "message": str(exc), "stepIndex": index, "stepType": step.type}
                steps.append(StepResult(index, step.type, "failed", started, core.now_iso(), [], failure))
                break
            except Exception as exc:
                failure = {"category": "runtime", "message": str(exc), "stepIndex": index, "stepType": step.type}
                steps.append(StepResult(index, step.type, "failed", started, core.now_iso(), [], failure))
                break
        observations = _final_observations(resolved, sandbox, state, steps)
        raw_path, normalized_path = write_observations(sandbox, observations)
        debug_dir = write_debug_bundle(resolved, sandbox, observations, failure) if failure else None
        return ScenarioResult(resolved.id, "failed" if failure else "passed", sandbox.run_id, str(sandbox.run_dir), steps, raw_path, normalized_path, debug_dir, failure)
    finally:
        sandbox.cleanup_sandbox(passed=failure is None)
        core.resetConfiguredRuntimeHome()


def run_scenario(scenario: Scenario | str, **kwargs: Any) -> ScenarioResult:
    return asyncio.run(run_scenario_async(scenario, **kwargs))


async def _execute_step(
    step_type: str,
    data: dict[str, Any],
    scenario: Scenario,
    sandbox: ScenarioSandbox,
    state: dict[str, Any],
    steps: list[StepResult],
    observations: dict[str, Any],
) -> list[str]:
    if step_type == "create_session":
        config = await core.resolveConfig(
            {
                "provider": data.get("provider", "openai"),
                "model": data.get("model", "gpt-5.4"),
                "permissionMode": data.get("permissionMode", "default"),
                "runtimeHome": str(sandbox.home),
                "settings": {},
            },
            str(sandbox.workspace),
        )
        session = core.createEmptySession(
            cwd=str(sandbox.workspace),
            provider=config["provider"],
            providerProfileName=config.get("providerProfileName"),
            model=config["model"],
            effort=config.get("effort"),
            systemPrompt=config["systemPrompt"],
            permissionMode=data.get("permissionMode") or config.get("permissionMode", "default"),
        )
        state["config"] = config
        state["session"] = session
        return ["session"]
    if step_type == "save_session":
        await core.saveSession(_current_session(state))
        return ["session"]
    if step_type == "load_session":
        session_id = data.get("sessionId") or _current_session(state)["id"]
        loaded = await core.loadSession(session_id)
        if not loaded:
            raise ScenarioAssertionError(f"Session not found: {session_id}", category="runtime")
        state["session"] = loaded
        return ["session"]
    if step_type == "assert_session":
        assert_session(state.get("session"), data)
        return ["session"]
    if step_type == "assert_file_exists":
        assert_file_exists(sandbox, validate_workspace_rel_path(data["path"]))
        return ["workspace_tree"]
    if step_type == "assert_observation":
        assert_observation(observations, data)
        return ["observations"]
    if step_type == "capture":
        include = data.get("include") or scenario.capture.get("include") or []
        observations.clear()
        observations.update(collect_observations(scenario, sandbox, state, steps, include))
        return sorted(observations)
    if step_type == "run_python":
        return await _run_python_action(data, sandbox, state)
    raise ScenarioAssertionError(f"Unsupported step type: {step_type}", category="harness")


async def _run_python_action(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    action = data.get("action")
    if action == "resolve_config":
        state["config"] = await core.resolveConfig({"runtimeHome": str(sandbox.home), **(data.get("options") or {})}, str(sandbox.workspace))
        return ["config"]
    raise ScenarioAssertionError(f"Unknown run_python action: {action}", category="harness")


def _current_session(state: dict[str, Any]) -> dict[str, Any]:
    session = state.get("session")
    if not isinstance(session, dict):
        raise ScenarioAssertionError("No current session is available", category="runtime")
    return session


def _final_observations(scenario: Scenario, sandbox: ScenarioSandbox, state: dict[str, Any], steps: list[StepResult]) -> dict[str, Any]:
    include = scenario.capture.get("include") or ["session", "step_results"]
    return collect_observations(scenario, sandbox, state, steps, include)
