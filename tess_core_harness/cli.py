from __future__ import annotations

import argparse
import json
import sys

from .ci_runner import run_ci_suite
from .event_corpus import exportEventCorpus, verifyEventCorpus
from .executor import run_scenario
from .fault_matrix import runAnthropicFaultMatrix, runProviderFaultMatrix, writeFaultMatrixReport
from .loader import load_scenarios
from .model import ScenarioValidationError
from .performance import (
    runAnthropicTransportBenchmark,
    runPersistenceBenchmark,
    runRollbackSnapshotBenchmark,
    writePerformanceReport,
)
from .performance_lane import runPerformanceLane, writePerformanceLaneReport
from .suite_speed import runSuiteSpeedLane, writeSuiteSpeedReport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tess_core_harness")
    parser.add_argument("--scenarios-dir")
    parser.add_argument("--runs-dir")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("scenario_id")
    run_parser.add_argument("--cleanup", choices=["never", "success"], default="never")
    run_parser.add_argument("--golden", choices=["off", "compare", "update"], default="off")
    run_parser.add_argument("--goldens-dir")
    ci_parser = subparsers.add_parser("ci")
    ci_parser.add_argument("--scenario", action="append", dest="scenario_ids")
    ci_parser.add_argument("--area", action="append", dest="areas")
    ci_parser.add_argument("--changed-area", action="append", dest="changed_areas")
    ci_parser.add_argument("--reports-dir")
    ci_parser.add_argument("--cleanup", choices=["never", "success"], default="success")
    ci_parser.add_argument("--golden", choices=["off", "compare", "update"], default="compare")
    ci_parser.add_argument("--goldens-dir")
    ci_parser.add_argument("--disable-capability", action="append", dest="disabled_capabilities")
    ci_parser.add_argument("--fail-fast", action="store_true")
    benchmark_parser = subparsers.add_parser("benchmark")
    benchmark_parser.add_argument("benchmark", choices=["persistence", "rollback", "transport"])
    benchmark_parser.add_argument("--message-counts", default="200,1000,5000")
    benchmark_parser.add_argument("--samples", type=int, default=5)
    benchmark_parser.add_argument("--persists-per-sample", type=int, default=3)
    benchmark_parser.add_argument("--content-bytes", type=int, default=1536)
    benchmark_parser.add_argument("--calls-per-turn", type=int, default=3)
    benchmark_parser.add_argument("--setup-delay-ms", type=int, default=80)
    benchmark_parser.add_argument("--output")
    fault_parser = subparsers.add_parser("fault-matrix")
    fault_parser.add_argument("provider", choices=["all", "anthropic"], default="all", nargs="?")
    fault_parser.add_argument("--output")
    corpus_parser = subparsers.add_parser("export-event-corpus")
    corpus_parser.add_argument("--output-dir")
    corpus_parser.add_argument("--check", action="store_true")
    lane_parser = subparsers.add_parser("performance-lane")
    lane_parser.add_argument("--quick", action="store_true")
    lane_parser.add_argument("--inject-regression", action="store_true")
    lane_parser.add_argument("--output")
    suite_parser = subparsers.add_parser("suite-speed")
    suite_parser.add_argument("--workers", type=int, default=4)
    suite_parser.add_argument("--target-seconds", type=float, default=150.0)
    suite_parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _list(args.scenarios_dir)
        if args.command == "run":
            return _run(args)
        if args.command == "ci":
            return _ci(args)
        if args.command == "benchmark":
            return _benchmark(args)
        if args.command == "fault-matrix":
            return _fault_matrix(args)
        if args.command == "export-event-corpus":
            return _event_corpus(args)
        if args.command == "performance-lane":
            return _performance_lane(args)
        if args.command == "suite-speed":
            return _suite_speed(args)
    except ScenarioValidationError as exc:
        print(f"harness validation error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"harness internal error: {exc}", file=sys.stderr)
        return 3
    return 2


def _list(scenarios_dir: str | None) -> int:
    for scenario in load_scenarios(scenarios_dir):
        print(f"{scenario.id}\t{scenario.title}")
    return 0


def _run(args: argparse.Namespace) -> int:
    result = run_scenario(
        args.scenario_id,
        scenarios_dir=args.scenarios_dir,
        runs_dir=args.runs_dir,
        cleanup=args.cleanup,
        golden=args.golden,
        goldens_dir=args.goldens_dir,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if result.status == "passed":
        return 0
    failure = result.failure or {}
    print(
        f"scenario failed: {result.scenarioId} step={failure.get('stepIndex')} type={failure.get('stepType')} error={failure.get('message')}",
        file=sys.stderr,
    )
    if result.debugDir:
        print(f"debug bundle: {result.debugDir}", file=sys.stderr)
    return 1


def _ci(args: argparse.Namespace) -> int:
    result = run_ci_suite(
        scenarios_dir=args.scenarios_dir,
        runs_dir=args.runs_dir,
        reports_dir=args.reports_dir,
        scenario_ids=args.scenario_ids,
        areas=args.areas,
        changed_areas=args.changed_areas,
        cleanup=args.cleanup,
        golden=args.golden,
        goldens_dir=args.goldens_dir,
        disabled_capabilities=args.disabled_capabilities,
        fail_fast=args.fail_fast,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if result.status == "passed":
        return 0
    print(f"ci suite failed: report={result.reports.get('markdown')}", file=sys.stderr)
    return 1


def _benchmark(args: argparse.Namespace) -> int:
    if args.benchmark == "transport":
        report = runAnthropicTransportBenchmark(
            samples=args.samples,
            calls_per_turn=args.calls_per_turn,
            connection_setup_delay_ms=args.setup_delay_ms,
        )
    elif args.benchmark == "rollback":
        message_counts = [int(value.strip()) for value in args.message_counts.split(",") if value.strip()]
        report = runRollbackSnapshotBenchmark(
            message_counts=message_counts,
            samples=args.samples,
            content_bytes=args.content_bytes,
        )
    else:
        message_counts = [int(value.strip()) for value in args.message_counts.split(",") if value.strip()]
        report = runPersistenceBenchmark(
            message_counts=message_counts,
            samples=args.samples,
            persists_per_sample=args.persists_per_sample,
            content_bytes=args.content_bytes,
        )
    if args.output:
        writePerformanceReport(report, args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _fault_matrix(args: argparse.Namespace) -> int:
    report = runAnthropicFaultMatrix() if args.provider == "anthropic" else runProviderFaultMatrix()
    if args.output:
        writeFaultMatrixReport(report, args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


def _event_corpus(args: argparse.Namespace) -> int:
    if args.check:
        report = verifyEventCorpus(args.output_dir) if args.output_dir else verifyEventCorpus()
    else:
        report = exportEventCorpus(args.output_dir) if args.output_dir else exportEventCorpus()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status", "passed") == "passed" else 1


def _performance_lane(args: argparse.Namespace) -> int:
    report = runPerformanceLane(inject_regression=args.inject_regression, quick=args.quick)
    if args.output:
        writePerformanceLaneReport(report, args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


def _suite_speed(args: argparse.Namespace) -> int:
    report = runSuiteSpeedLane(workers=args.workers, target_seconds=args.target_seconds)
    if args.output:
        writeSuiteSpeedReport(report, args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1
