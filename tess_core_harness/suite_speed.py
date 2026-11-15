from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .paths import REPO_ROOT

SUITE_SPEED_SCHEMA = "tess-core.suite-speed.v1"
PROTOCOL_PROCESS_FILES = (
    "test_contracts_typecheck.py",
    "test_headless_cli.py",
    "test_mcp_header_origin.py",
    "test_mcp_runtime_edges.py",
    "test_mcp_stdio_sse.py",
    "test_mcp_cancellation_blob_plugin.py",
    "test_p01_launch_concurrency.py",
    "test_p01_spawn_before_persist_probe.py",
    "test_package28_mcp_oauth.py",
    "test_package31_mcp_transport.py",
    "test_package31_web_acquisition.py",
    "test_package32_anthropic_transport.py",
    "test_package32_prompt_cache_state.py",
    "test_package45_transport.py",
    "test_providers_anthropic.py",
    "test_providers_anthropic_streaming.py",
    "test_sandbox.py",
    "test_tools_agent.py",
    "test_tools_worktree_notebook.py",
)
TASK_PROCESS_FILES = (
    "test_harness_task_driver.py",
    "test_package31_daemon_protocol_security.py",
    "test_package31_health_check_safety.py",
    "test_package31_monitor_transition_concurrency.py",
    "test_package31_task_stop_terminal.py",
    "test_task_identity_ownership.py",
    "test_task_launch_reconciliation.py",
    "test_task_launch_reservation.py",
    "test_task_monitor_health.py",
    "test_task_supervisor_core.py",
    "test_task_supervisor_lifecycle.py",
    "test_task_supervisor_reconciliation.py",
    "test_task_supervisor_runtime_home.py",
)
SCENARIO_PROCESS_FILES = (
    "test_coordination_transaction_migrations.py",
    "test_harness_ci_runner.py",
    "test_harness_context_driver.py",
    "test_harness_executor.py",
    "test_harness_fake_providers.py",
    "test_harness_goldens.py",
    "test_harness_loader.py",
    "test_harness_mcp_driver.py",
    "test_harness_remote_driver.py",
    "test_harness_runtime_driver.py",
    "test_harness_sandbox.py",
    "test_harness_storage_driver.py",
    "test_p02_concurrent_daemon_bootstrap.py",
    "test_package31_daemon_transport.py",
    "test_package31_remote_bridge_lifecycle.py",
    "test_remote_bridge.py",
    "test_runtime_bootstrap_lifecycle.py",
    "test_runtime_bootstrap_plugins.py",
    "test_shell_authority_parser.py",
    "test_shell_commands.py",
    "test_shell_remote_background.py",
)
PROCESS_FILES = PROTOCOL_PROCESS_FILES + TASK_PROCESS_FILES + SCENARIO_PROCESS_FILES
TIMING_FILES = (
    "test_package29_store_scale.py",
    "test_package31_token_count_deadlines.py",
    "test_package43_codex_supersession.py",
    "test_package43_review_regressions.py",
    "test_package47b_truth_lanes.py",
    "test_provider_errors.py",
    "test_providers_anthropic_transport.py",
    "test_providers_openai_responses.py",
    "test_record_corruption_isolation.py",
    "test_storage_concurrency.py",
    "test_storage_runtime_home.py",
)
CRITICAL_TIMING_FILES = ("test_package46_persistence_efficiency.py",)


def runSuiteSpeedLane(*, workers: int = 4, target_seconds: float = 150.0) -> dict[str, Any]:
    if workers < 1 or target_seconds <= 0:
        raise ValueError("workers and target_seconds must be positive")
    with tempfile.TemporaryDirectory(prefix="tess-core-suite-speed-") as tmp:
        report_root = Path(tmp)
        baseline = _run_serial_baseline(report_root / "serial-baseline.xml")
        started = time.perf_counter()
        critical = _run_file_group(
            "serial-event-loop-stall",
            CRITICAL_TIMING_FILES,
            report_root / "critical-timing.xml",
            False,
        )
        process = _run_process_groups(report_root)
        serial = _run_serial(report_root / "serial.xml")
        parallel = _run_parallel(workers, report_root)
        wall_seconds = round(time.perf_counter() - started, 3)
        qualification_seconds = round(time.perf_counter() - (started - baseline["elapsedSeconds"]), 3)
    optimized_phases = [critical, *parallel, *process, serial]
    optimized_outcomes, duplicate_cases = _merge_outcomes(optimized_phases)
    baseline_outcomes = baseline.pop("_outcomes")
    for phase in optimized_phases:
        phase.pop("_outcomes", None)
    missing_cases = sorted(set(baseline_outcomes) - set(optimized_outcomes))
    extra_cases = sorted(set(optimized_outcomes) - set(baseline_outcomes))
    outcome_mismatches = [
        {
            "test": case,
            "serial": baseline_outcomes[case],
            "optimized": optimized_outcomes[case],
        }
        for case in sorted(set(baseline_outcomes) & set(optimized_outcomes))
        if baseline_outcomes[case] != optimized_outcomes[case]
    ]
    phase_failures = [
        {"phase": phase["name"], "returnCode": phase["returnCode"], "tests": phase["tests"]}
        for phase in optimized_phases
        if phase["returnCode"] != 0 or phase["tests"] == 0
    ]
    equivalent = not missing_cases and not extra_cases and not outcome_mismatches and not duplicate_cases
    clean = baseline["returnCode"] == 0 and not phase_failures
    within_target = wall_seconds < target_seconds
    return {
        "schemaVersion": SUITE_SPEED_SCHEMA,
        "status": "passed" if equivalent and clean and within_target else "failed",
        "workers": workers,
        "targetSeconds": target_seconds,
        "wallSeconds": wall_seconds,
        "qualificationSeconds": qualification_seconds,
        "withinTarget": within_target,
        "serialEquivalent": equivalent,
        "cleanOutcomes": clean,
        "collection": {
            "serialCount": len(baseline_outcomes),
            "optimizedCount": len(optimized_outcomes),
            "serialDigest": _outcome_digest(baseline_outcomes, include_outcome=False),
            "optimizedDigest": _outcome_digest(optimized_outcomes, include_outcome=False),
            "missing": missing_cases,
            "extra": extra_cases,
            "duplicates": duplicate_cases,
        },
        "outcomeEquivalence": {
            "serialDigest": _outcome_digest(baseline_outcomes, include_outcome=True),
            "optimizedDigest": _outcome_digest(optimized_outcomes, include_outcome=True),
            "mismatches": outcome_mismatches,
        },
        "phaseFailures": phase_failures,
        "baseline": baseline,
        "phases": optimized_phases,
    }


def writeSuiteSpeedReport(report: dict[str, Any], output: str | Path) -> str:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def _run_parallel(workers: int, report_root: Path) -> list[dict[str, Any]]:
    excluded = set(PROCESS_FILES + TIMING_FILES + CRITICAL_TIMING_FILES)
    files = [
        path
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py"))
        if path.name not in excluded and _test_weight(path) > 0
    ]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="suite-bulk") as pool:
        futures = [
            pool.submit(
                _run_file_group,
                f"parallel-{path.stem}",
                (str(path.relative_to(REPO_ROOT / "tests")),),
                report_root / f"parallel-{index + 1}.xml",
                False,
            )
            for index, path in enumerate(sorted(files, key=_test_weight, reverse=True))
        ]
        return [future.result() for future in futures]


def _run_process_groups(report_root: Path) -> list[dict[str, Any]]:
    groups = (
        ("process-protocol", PROTOCOL_PROCESS_FILES, report_root / "process-protocol.xml"),
        ("process-task", TASK_PROCESS_FILES, report_root / "process-task.xml"),
    )
    phases = [
        _run_file_group(name, files, junit_path)
        for name, files, junit_path in groups
    ]
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="suite-scenario") as pool:
        futures = [
            pool.submit(
                _run_file_group,
                f"scenario-{index + 1}",
                (file_name,),
                report_root / f"scenario-{index + 1}.xml",
                False,
            )
            for index, file_name in enumerate(SCENARIO_PROCESS_FILES)
        ]
        phases.extend(future.result() for future in futures)
    return phases


def _run_file_group(
    name: str,
    files: tuple[str, ...],
    junit_path: Path,
    use_xdist: bool = True,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        f"--junitxml={junit_path}",
    ]
    if use_xdist:
        command[5:5] = ["-n", "2", "--dist", "loadscope"]
    command.extend(f"tests/{file_name}" for file_name in files)
    return _run_phase(name, command, junit_path)


def _test_weight(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("def test_")


def _run_serial(junit_path: Path) -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", "-q", f"--junitxml={junit_path}"]
    command.extend(f"tests/{name}" for name in TIMING_FILES)
    return _run_phase("serial-tight-timing", command, junit_path)


def _run_serial_baseline(junit_path: Path) -> dict[str, Any]:
    return _run_phase(
        "serial-reference",
        [sys.executable, "-m", "pytest", "-q", f"--junitxml={junit_path}", "tests"],
        junit_path,
    )


def _run_phase(name: str, command: list[str], junit_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    elapsed = round(time.perf_counter() - started, 3)
    if not junit_path.exists():
        return {
            "name": name,
            "status": "failed",
            "elapsedSeconds": elapsed,
            "returnCode": completed.returncode,
            "tests": 0,
            "failures": [],
            "errors": 1,
            "skipped": 0,
            "collectionDigest": _outcome_digest({}, include_outcome=False),
            "outcomeDigest": _outcome_digest({}, include_outcome=True),
            "_outcomes": {},
            "stderrTail": completed.stderr[-2_000:],
        }
    parsed = _parse_junit(junit_path)
    return {
        "name": name,
        "status": "passed" if completed.returncode == 0 else "test-failures",
        "elapsedSeconds": elapsed,
        "returnCode": completed.returncode,
        **parsed,
    }


def _parse_junit(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = [case.get("name", "") for case in cases if case.find("failure") is not None]
    errors = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    outcomes: dict[str, str] = {}
    for case in cases:
        identity = _junit_case_id(case)
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        if identity in outcomes:
            identity = f"{identity}#duplicate-{sum(key.startswith(identity) for key in outcomes) + 1}"
        outcomes[identity] = outcome
    return {
        "tests": len(cases),
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "collectionDigest": _outcome_digest(outcomes, include_outcome=False),
        "outcomeDigest": _outcome_digest(outcomes, include_outcome=True),
        "_outcomes": outcomes,
    }


def _junit_case_id(case: ET.Element) -> str:
    source = case.get("file") or case.get("classname") or "unknown"
    return f"{source}::{case.get('classname') or ''}::{case.get('name') or ''}"


def _merge_outcomes(phases: list[dict[str, Any]]) -> tuple[dict[str, str], list[str]]:
    merged: dict[str, str] = {}
    duplicates: list[str] = []
    for phase in phases:
        for identity, outcome in phase.get("_outcomes", {}).items():
            if identity in merged:
                duplicates.append(identity)
            else:
                merged[identity] = outcome
    return merged, sorted(duplicates)


def _outcome_digest(outcomes: dict[str, str], *, include_outcome: bool) -> str:
    lines = [
        f"{identity}={outcome}" if include_outcome else identity
        for identity, outcome in sorted(outcomes.items())
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


run_suite_speed_lane = runSuiteSpeedLane
