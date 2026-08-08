from __future__ import annotations

import asyncio
import json
from typing import Any

import agent_core as core

from .assertions import assert_file_exists, assert_observation, assert_session, validate_workspace_rel_path
from .capture import collect_observations, write_observations
from .context_driver import (
    apply_context_relief,
    assert_compaction_artifact,
    assert_context_result,
    assert_context_runtime_state,
    project_context_events,
    run_context_compaction,
    run_context_fallback,
    run_reactive_context_cancellation,
    run_reactive_context_retry,
    seed_context_session,
)
from .debug_bundle import write_debug_bundle
from .fake_providers import FakeProviderRuntime, assert_provider_capture, assert_provider_response
from .fault_matrix import run_anthropic_fault_matrix_step
from .goldens import compare_golden, expected_observations, render_diff, update_golden
from .loader import load_scenario
from agent_core._utils import now_iso
from .mcp_driver import (
    assert_mcp_bootstrap,
    assert_mcp_event,
    assert_mcp_result,
    assert_mcp_status,
    bootstrap_mcp_plugin_runtime,
    call_mcp_generated_tool,
    call_mcp_tool,
    close_mcp_driver,
    close_mcp_runtime,
    create_mcp_plugin_fixture,
    create_mcp_runtime,
    list_mcp_resources,
    list_mcp_tools,
    read_mcp_resource,
    start_fake_mcp,
)
from .mcp_package28 import (
    apply_mcp_catalog_change,
    record_mcp_protocol_probe,
    recover_mcp_headless_auth,
    run_mcp_dispatch_probe,
)
from .model import Scenario, ScenarioAssertionError, ScenarioResult, StepResult
from .normalization import normalize_observations
from .remote_driver import (
    assert_bridge_diagnostics,
    assert_collaboration_snapshot,
    assert_remote_result,
    assert_remote_runtime_state,
    bootstrap_remote_runtime,
    close_bridge_runtime,
    close_remote_driver,
    collect_bridge_diagnostics,
    create_remote_bridge,
    poll_bridge_inbox,
    project_remote_events,
    queue_bridge_message,
    reconnect_bridge,
    refresh_collaboration,
    seed_collaboration_fixture,
    set_bridge_emit_failure,
    update_child_agent_status,
)
from .runtime_driver import (
    assert_driver_result,
    assert_runtime_event,
    bootstrap_runtime,
    call_tool,
    close_runtime_driver,
    run_agent,
    run_slash_command,
    run_subprocess,
)
from .sandbox import ScenarioSandbox
from .task_driver import (
    assert_bootstrap,
    assert_daemon_event,
    assert_task_output,
    assert_task_record,
    close_task_driver,
    create_monitor,
    daemon_subscribe,
    ensure_daemon,
    get_daemon_bootstrap,
    read_task_output,
    restart_daemon,
    run_worker_entrypoint,
    seed_recovery_task,
    start_background_task,
    start_health_check,
    stop_task,
    wait_health_check,
    wait_task_status,
)


async def run_scenario_async(
    scenario: Scenario | str,
    *,
    scenarios_dir: str | None = None,
    runs_dir: str | None = None,
    cleanup: str = "never",
    golden: str = "off",
    goldens_dir: str | None = None,
) -> ScenarioResult:
    resolved = load_scenario(scenario, scenarios_dir) if isinstance(scenario, str) else scenario
    sandbox = ScenarioSandbox(resolved.id, cleanup=cleanup, **({"runs_dir": runs_dir} if runs_dir else {})).create()
    steps: list[StepResult] = []
    state: dict[str, Any] = {}
    observations: dict[str, Any] = {}
    failure: dict[str, Any] | None = None
    golden_result: dict[str, Any] | None = None
    debug_extras: dict[str, str] = {}
    raw_path = ""
    normalized_path = ""
    try:
        sandbox.apply_setup(resolved.setup)
        for index, step in enumerate(resolved.steps):
            started = now_iso()
            try:
                observation_keys = await _execute_step(step.type, step.input, resolved, sandbox, state, steps, observations)
                steps.append(StepResult(index, step.type, "passed", started, now_iso(), observation_keys))
            except ScenarioAssertionError as exc:
                failure = {"category": exc.category, "message": str(exc), "stepIndex": index, "stepType": step.type}
                steps.append(StepResult(index, step.type, "failed", started, now_iso(), [], failure))
                break
            except Exception as exc:
                failure = {"category": "runtime", "message": str(exc), "stepIndex": index, "stepType": step.type}
                steps.append(StepResult(index, step.type, "failed", started, now_iso(), [], failure))
                break
        observations = _final_observations(resolved, sandbox, state, steps)
        raw_path, normalized_path = write_observations(sandbox, observations)
        if failure is None and golden != "off":
            normalized = normalize_observations(observations, sandbox)
            golden_result, failure, debug_extras = _handle_golden(resolved, sandbox, normalized, golden, goldens_dir)
        debug_dir = write_debug_bundle(resolved, sandbox, observations, failure, debug_extras) if failure else None
        return ScenarioResult(
            resolved.id,
            "failed" if failure else "passed",
            sandbox.run_id,
            str(sandbox.run_dir),
            steps,
            raw_path,
            normalized_path,
            golden_result,
            debug_dir,
            failure,
        )
    finally:
        provider_runtime = state.get("provider_runtime")
        if provider_runtime:
            provider_runtime.close()
        await close_mcp_driver(state)
        await close_task_driver(state)
        await close_remote_driver(state)
        await close_runtime_driver(state)
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
    if step_type == "provider_request":
        runtime = state.setdefault("provider_runtime", FakeProviderRuntime())
        result = await runtime.run(data)
        state["provider_results"] = runtime.results
        state["provider_last_result"] = result
        return ["provider_response", "provider_requests", "provider_events"]
    if step_type == "run_anthropic_fault_matrix":
        return await run_anthropic_fault_matrix_step(data, sandbox, state)
    if step_type == "seed_context_session":
        return await seed_context_session(data, sandbox, state)
    if step_type == "run_context_compaction":
        return await run_context_compaction(data, sandbox, state)
    if step_type == "run_context_fallback":
        return await run_context_fallback(data, sandbox, state)
    if step_type == "apply_context_relief":
        return await apply_context_relief(data, sandbox, state)
    if step_type == "project_context_events":
        return await project_context_events(data, sandbox, state)
    if step_type == "run_reactive_context_retry":
        return await run_reactive_context_retry(data, sandbox, state)
    if step_type == "run_reactive_context_cancellation":
        return await run_reactive_context_cancellation(data, sandbox, state)
    if step_type == "bootstrap_runtime":
        return await bootstrap_runtime(data, sandbox, state)
    if step_type == "seed_collaboration_fixture":
        return await seed_collaboration_fixture(data, sandbox, state)
    if step_type == "create_remote_bridge":
        return await create_remote_bridge(data, sandbox, state)
    if step_type == "queue_bridge_message":
        return await queue_bridge_message(data, sandbox, state)
    if step_type == "poll_bridge_inbox":
        return await poll_bridge_inbox(data, sandbox, state)
    if step_type == "set_bridge_emit_failure":
        return await set_bridge_emit_failure(data, sandbox, state)
    if step_type == "close_bridge_runtime":
        return await close_bridge_runtime(data, sandbox, state)
    if step_type == "reconnect_bridge":
        return await reconnect_bridge(data, sandbox, state)
    if step_type == "collect_bridge_diagnostics":
        return await collect_bridge_diagnostics(data, sandbox, state)
    if step_type == "refresh_collaboration":
        return await refresh_collaboration(data, sandbox, state)
    if step_type == "update_child_agent_status":
        return await update_child_agent_status(data, sandbox, state)
    if step_type == "bootstrap_remote_runtime":
        return await bootstrap_remote_runtime(data, sandbox, state)
    if step_type == "project_remote_events":
        return await project_remote_events(data, sandbox, state)
    if step_type == "start_fake_mcp":
        return await start_fake_mcp(data, sandbox, state)
    if step_type == "create_mcp_runtime":
        return await create_mcp_runtime(data, sandbox, state)
    if step_type == "list_mcp_tools":
        return await list_mcp_tools(data, sandbox, state)
    if step_type == "list_mcp_resources":
        return await list_mcp_resources(data, sandbox, state)
    if step_type == "read_mcp_resource":
        return await read_mcp_resource(data, sandbox, state)
    if step_type == "call_mcp_tool":
        return await call_mcp_tool(data, sandbox, state)
    if step_type == "call_mcp_generated_tool":
        return await call_mcp_generated_tool(data, sandbox, state)
    if step_type == "run_mcp_dispatch_probe":
        return await run_mcp_dispatch_probe(data, sandbox, state)
    if step_type == "apply_mcp_catalog_change":
        return await apply_mcp_catalog_change(data, sandbox, state)
    if step_type == "record_mcp_protocol_probe":
        return await record_mcp_protocol_probe(data, sandbox, state)
    if step_type == "recover_mcp_headless_auth":
        return await recover_mcp_headless_auth(data, sandbox, state)
    if step_type == "create_mcp_plugin_fixture":
        return await create_mcp_plugin_fixture(data, sandbox, state)
    if step_type == "bootstrap_mcp_plugin_runtime":
        return await bootstrap_mcp_plugin_runtime(data, sandbox, state)
    if step_type == "close_mcp_runtime":
        return await close_mcp_runtime(data, sandbox, state)
    if step_type == "ensure_daemon":
        return await ensure_daemon(data, sandbox, state)
    if step_type == "daemon_subscribe":
        return await daemon_subscribe(data, sandbox, state)
    if step_type == "start_background_task":
        return await start_background_task(data, sandbox, state)
    if step_type == "wait_task_status":
        return await wait_task_status(data, sandbox, state)
    if step_type == "read_task_output":
        return await read_task_output(data, sandbox, state)
    if step_type == "stop_task":
        return await stop_task(data, sandbox, state)
    if step_type == "create_monitor":
        return await create_monitor(data, sandbox, state)
    if step_type == "get_daemon_bootstrap":
        return await get_daemon_bootstrap(data, sandbox, state)
    if step_type == "seed_recovery_task":
        return await seed_recovery_task(data, sandbox, state)
    if step_type == "restart_daemon":
        return await restart_daemon(data, sandbox, state)
    if step_type == "run_worker_entrypoint":
        return await run_worker_entrypoint(data, sandbox, state)
    if step_type == "start_health_check":
        return await start_health_check(data, sandbox, state)
    if step_type == "wait_health_check":
        return await wait_health_check(data, sandbox, state)
    if step_type == "run_agent":
        return await run_agent(data, sandbox, state)
    if step_type == "run_subprocess":
        return await run_subprocess(data, sandbox, state)
    if step_type == "run_slash_command":
        return await run_slash_command(data, sandbox, state)
    if step_type == "call_tool":
        return await call_tool(data, sandbox, state)
    if step_type == "assert_provider_response":
        assert_provider_response(state, data)
        return ["provider_response"]
    if step_type == "assert_provider_capture":
        assert_provider_capture(state, data)
        return ["provider_requests", "provider_events"]
    if step_type == "assert_context_result":
        assert_context_result(state, data)
        return ["compaction_results", "continuation_results", "context_agent_results", "context_events"]
    if step_type == "assert_compaction_artifact":
        assert_compaction_artifact(state, data)
        return ["compaction_artifacts", "durable_memory"]
    if step_type == "assert_context_runtime_state":
        assert_context_runtime_state(state, data)
        return ["context_runtime_state"]
    if step_type == "assert_bridge_diagnostics":
        assert_bridge_diagnostics(state, data)
        return ["bridge_diagnostics", "bridge_events", "bridge_records"]
    if step_type == "assert_collaboration_snapshot":
        assert_collaboration_snapshot(state, data)
        return ["collaboration_snapshots", "collaboration_events"]
    if step_type == "assert_remote_runtime_state":
        assert_remote_runtime_state(state, data)
        return ["remote_runtime_state"]
    if step_type == "assert_remote_result":
        assert_remote_result(state, data)
        return ["remote_driver_results"]
    if step_type == "assert_driver_result":
        assert_driver_result(state, data)
        return ["driver_results"]
    if step_type == "assert_task_record":
        assert_task_record(state, data)
        return ["task_records", "task_metadata"]
    if step_type == "assert_daemon_event":
        assert_daemon_event(state, data)
        return ["daemon_events"]
    if step_type == "assert_task_output":
        assert_task_output(state, data)
        return ["task_outputs"]
    if step_type == "assert_bootstrap":
        assert_bootstrap(state, data)
        return ["daemon_bootstrap"]
    if step_type == "assert_runtime_event":
        assert_runtime_event(state, data)
        return ["runtime_events"]
    if step_type == "assert_mcp_status":
        assert_mcp_status(state, data)
        return ["mcp_status"]
    if step_type == "assert_mcp_event":
        return await assert_mcp_event(data, sandbox, state)
    if step_type == "assert_mcp_result":
        assert_mcp_result(state, data)
        return ["mcp_results"]
    if step_type == "assert_mcp_bootstrap":
        assert_mcp_bootstrap(state, data)
        return ["mcp_plugin_results"]
    if step_type == "capture":
        include = data.get("include") or scenario.capture.get("include") or []
        observations.clear()
        observations.update(collect_observations(scenario, sandbox, state, steps, include))
        return sorted(observations)
    if step_type == "parity_action":
        raise ScenarioAssertionError("parity_action steps must be run with the parity command", category="harness")
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


def _handle_golden(
    scenario: Scenario,
    sandbox: ScenarioSandbox,
    normalized: dict[str, Any],
    mode: str,
    goldens_dir: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, str]]:
    if mode == "update":
        return update_golden(scenario, normalized, goldens_dir), None, {}
    if mode != "compare":
        raise ScenarioAssertionError(f"Unsupported golden mode: {mode}", category="harness")
    diff_path = sandbox.debug / "golden.diff"
    result = compare_golden(scenario, normalized, goldens_dir=goldens_dir, diff_path=diff_path)
    if result.get("status") == "matched":
        return result, None, {}
    expected = expected_observations(scenario, goldens_dir)
    extras: dict[str, str] = {}
    if expected is not None:
        extras["expected-observations.normalized.json"] = json.dumps(expected, indent=2, sort_keys=True) + "\n"
        extras["golden.diff"] = diff_path.read_text(encoding="utf-8") if diff_path.exists() else render_diff(expected, normalized)
    failure = {
        "category": "golden",
        "message": result.get("message", "Golden comparison failed."),
        "stepIndex": None,
        "stepType": "golden_compare",
    }
    return result, failure, extras
