from __future__ import annotations

import json
import os
import shlex
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .executor import run_scenario
from .loader import load_scenarios
from .model import Scenario, ScenarioValidationError
from .paths import DEFAULT_RUNS_DIR


@dataclass
class SuiteRecord:
    scenarioId: str
    title: str
    area: str
    mode: str
    status: str
    durationSeconds: float = 0.0
    reason: str | None = None
    runDir: str | None = None
    debugDir: str | None = None
    observationsPath: str | None = None
    normalizedPath: str | None = None
    replayCommand: str | None = None
    goldenUpdateCommand: str | None = None
    failure: dict[str, Any] | None = None
    golden: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value is not None}


@dataclass
class SuiteResult:
    runId: str
    status: str
    reportDir: str
    reports: dict[str, str]
    counts: dict[str, int]
    records: list[SuiteRecord] = field(default_factory=list)
    selectors: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.runId,
            "status": self.status,
            "reportDir": self.reportDir,
            "reports": self.reports,
            "counts": self.counts,
            "selectors": self.selectors,
            "records": [record.to_dict() for record in self.records],
        }


def run_ci_suite(
    *,
    scenarios_dir: str | None = None,
    runs_dir: str | None = None,
    reports_dir: str | None = None,
    scenario_ids: list[str] | None = None,
    areas: list[str] | None = None,
    changed_areas: list[str] | None = None,
    cleanup: str = "success",
    golden: str = "compare",
    goldens_dir: str | None = None,
    disabled_capabilities: list[str] | None = None,
    fail_fast: bool = False,
) -> SuiteResult:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    runs_root = Path(runs_dir) if runs_dir else DEFAULT_RUNS_DIR
    scenario_runs_dir = runs_root / run_id / "scenarios"
    report_dir = Path(reports_dir) if reports_dir else runs_root / run_id / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    scenarios = select_scenarios(load_scenarios(scenarios_dir), scenario_ids or [], areas or [], changed_areas or [])
    disabled = set(disabled_capabilities or [])
    capabilities = detect_capabilities(disabled)
    records: list[SuiteRecord] = []
    selectors = {"scenario_ids": scenario_ids or [], "areas": areas or [], "changed_areas": changed_areas or []}
    for scenario in scenarios:
        mode = scenario_mode(scenario)
        replay = command_for(scenario, mode, scenario_runs_dir, scenarios_dir, golden, "never", goldens_dir)
        update = command_for(scenario, mode, scenario_runs_dir, scenarios_dir, "update", "success", goldens_dir)
        missing = missing_capabilities(scenario, capabilities)
        if missing:
            records.append(SuiteRecord(scenario.id, scenario.title, scenario.area, mode, "skipped", reason=f"missing capabilities: {', '.join(missing)}", replayCommand=replay, goldenUpdateCommand=update))
            continue
        started = time.monotonic()
        result = _run_one(scenario, mode, str(scenario_runs_dir), scenarios_dir, cleanup, golden, goldens_dir)
        duration = round(time.monotonic() - started, 3)
        records.append(_record_from_result(scenario, mode, result, duration, replay, update))
        if fail_fast and result.status != "passed":
            break
    counts = count_records(records)
    status = "failed" if counts["failed"] else "passed"
    reports = write_reports(report_dir, run_id, status, records, counts, selectors)
    summary = SuiteResult(run_id, status, str(report_dir), reports, counts, records, selectors)
    Path(reports["json"]).write_text(json.dumps(summary.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def select_scenarios(scenarios: list[Scenario], scenario_ids: list[str], areas: list[str], changed_areas: list[str]) -> list[Scenario]:
    wanted_ids = set(scenario_ids)
    by_id = {scenario.id: scenario for scenario in scenarios}
    missing = sorted(wanted_ids - set(by_id))
    if missing:
        raise ScenarioValidationError(f"Unknown scenario id(s): {', '.join(missing)}")
    selected: list[Scenario] = []
    changed = set(changed_areas)
    area_set = set(areas)
    for scenario in scenarios:
        include = not wanted_ids and not area_set and not changed
        include = include or scenario.id in wanted_ids
        include = include or scenario.area in area_set
        include = include or (changed and scenario.area in changed)
        include = include or (changed and scenario.area == "smoke")
        if include:
            selected.append(scenario)
    return selected


def detect_capabilities(disabled: set[str] | None = None) -> dict[str, bool]:
    disabled = disabled or set()
    capabilities = {
        "python": True,
        "node": shutil.which("node") is not None,
        "local_processes": True,
        "daemon": True,
        "network_loopback": True,
    }
    for name in disabled:
        capabilities[name] = False
    return capabilities


def missing_capabilities(scenario: Scenario, capabilities: dict[str, bool]) -> list[str]:
    return [name for name in scenario.capabilities if not capabilities.get(name, False)]


def scenario_mode(scenario: Scenario) -> str:
    return "run"


def command_for(
    scenario: Scenario,
    mode: str,
    runs_dir: Path,
    scenarios_dir: str | None,
    golden: str,
    cleanup: str,
    goldens_dir: str | None,
) -> str:
    parts = ["python3", "-m", "tess_core_harness", "--runs-dir", str(runs_dir)]
    if scenarios_dir:
        parts += ["--scenarios-dir", scenarios_dir]
    parts += [mode, scenario.id, "--golden", golden, "--cleanup", cleanup]
    if goldens_dir:
        parts += ["--goldens-dir", goldens_dir]
    return " ".join(shlex.quote(str(part)) for part in parts)


def count_records(records: list[SuiteRecord]) -> dict[str, int]:
    return {
        "total": len(records),
        "passed": sum(1 for record in records if record.status == "passed"),
        "failed": sum(1 for record in records if record.status == "failed"),
        "skipped": sum(1 for record in records if record.status == "skipped"),
    }


def write_reports(report_dir: Path, run_id: str, status: str, records: list[SuiteRecord], counts: dict[str, int], selectors: dict[str, list[str]]) -> dict[str, str]:
    paths = {"json": str(report_dir / "summary.json"), "markdown": str(report_dir / "report.md"), "junit": str(report_dir / "junit.xml")}
    Path(paths["markdown"]).write_text(render_markdown(run_id, status, records, counts, selectors), encoding="utf-8")
    Path(paths["junit"]).write_text(render_junit(run_id, records), encoding="utf-8")
    return paths


def render_markdown(run_id: str, status: str, records: list[SuiteRecord], counts: dict[str, int], selectors: dict[str, list[str]]) -> str:
    lines = [
        f"# Tess Core Scenario Report",
        "",
        f"- Run ID: `{run_id}`",
        f"- Status: `{status}`",
        f"- Counts: {counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped, {counts['total']} total",
        f"- Selectors: `{json.dumps(selectors, sort_keys=True)}`",
        "",
        "| Status | Scenario | Area | Mode | Seconds | Replay | Golden Update |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for record in records:
        lines.append(
            f"| {record.status} | `{record.scenarioId}` | `{record.area}` | `{record.mode}` | {record.durationSeconds:.3f} | "
            f"`{record.replayCommand or ''}` | `{record.goldenUpdateCommand or ''}` |"
        )
    failures = [record for record in records if record.status == "failed"]
    skips = [record for record in records if record.status == "skipped"]
    if failures:
        lines += ["", "## Failures"]
        for record in failures:
            lines += [f"- `{record.scenarioId}`: {record.failure}", f"  - Debug: `{record.debugDir}`"]
    if skips:
        lines += ["", "## Skips"]
        for record in skips:
            lines.append(f"- `{record.scenarioId}`: {record.reason}")
    return "\n".join(lines) + "\n"


def render_junit(run_id: str, records: list[SuiteRecord]) -> str:
    suite = ET.Element("testsuite", {"name": "tess_core_harness", "id": run_id, "tests": str(len(records)), "failures": str(sum(1 for item in records if item.status == "failed")), "skipped": str(sum(1 for item in records if item.status == "skipped"))})
    for record in records:
        case = ET.SubElement(suite, "testcase", {"classname": f"tess_core_harness.{record.area}", "name": record.scenarioId, "time": f"{record.durationSeconds:.3f}"})
        if record.status == "failed":
            failure = ET.SubElement(case, "failure", {"message": (record.failure or {}).get("message", "scenario failed")})
            failure.text = json.dumps(record.failure or {}, indent=2, sort_keys=True)
        elif record.status == "skipped":
            skipped = ET.SubElement(case, "skipped", {"message": record.reason or "skipped"})
            skipped.text = record.reason or "skipped"
    return ET.tostring(suite, encoding="unicode") + "\n"


def _run_one(scenario: Scenario, mode: str, runs_dir: str, scenarios_dir: str | None, cleanup: str, golden: str, goldens_dir: str | None) -> Any:
    return run_scenario(scenario.id, scenarios_dir=scenarios_dir, runs_dir=runs_dir, cleanup=cleanup, golden=golden, goldens_dir=goldens_dir)


def _record_from_result(scenario: Scenario, mode: str, result: Any, duration: float, replay: str, update: str) -> SuiteRecord:
    return SuiteRecord(
        scenario.id,
        scenario.title,
        scenario.area,
        mode,
        result.status,
        duration,
        runDir=result.runDir,
        debugDir=result.debugDir,
        observationsPath=result.observationsPath,
        normalizedPath=result.normalizedPath,
        replayCommand=replay,
        goldenUpdateCommand=update,
        failure=result.failure,
        golden=result.golden,
    )
