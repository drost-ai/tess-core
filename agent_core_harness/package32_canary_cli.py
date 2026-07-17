from __future__ import annotations

import argparse
import json
import os
from typing import Any, Callable

from .package32_canary_evidence import Package32CanaryError


def runPackage32CanaryCli(
    run_canary: Callable[[dict[str, Any]], dict[str, Any]],
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_core_harness.package32_canary"
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--runtime-home", required=True)
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--max-cost-usd", required=True, type=float)
    parser.add_argument("--authorize-paid-provider-calls", action="store_true")
    args = parser.parse_args(argv)
    try:
        evidence = run_canary(
            {
                "authorized": args.authorize_paid_provider_calls,
                "model": args.model,
                "runtimeHome": args.runtime_home,
                "evidenceFile": args.evidence_file,
                "maxCostUsd": args.max_cost_usd,
                "apiKey": os.environ.get("ANTHROPIC_API_KEY"),
            }
        )
    except Package32CanaryError as error:
        print(f"package32 canary refused: {error.code}")
        return 2
    except BaseException:
        print("package32 canary failed: internal_failure")
        return 3
    print(
        json.dumps(
            {
                "status": evidence["status"],
                "actualCostUsd": evidence["actualCostUsd"],
            }
        )
    )
    return 0
