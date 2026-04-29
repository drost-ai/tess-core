from __future__ import annotations

import argparse
import json
import sys

from .executor import run_scenario
from .loader import load_scenarios
from .model import ScenarioValidationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_core_harness")
    parser.add_argument("--scenarios-dir")
    parser.add_argument("--runs-dir")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("scenario_id")
    run_parser.add_argument("--cleanup", choices=["never", "success"], default="never")
    run_parser.add_argument("--golden", choices=["off", "compare", "update"], default="off")
    run_parser.add_argument("--goldens-dir")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _list(args.scenarios_dir)
        if args.command == "run":
            return _run(args)
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
