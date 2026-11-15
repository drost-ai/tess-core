from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .cache_economics import runCacheEconomicLane
from .performance import (
    runAnthropicTransportBenchmark,
    runPersistenceBenchmark,
    runRollbackSnapshotBenchmark,
)
from .performance_economics import runPersistenceEconomics

LANE_SCHEMA = "tess-core.performance-lane.v1"
BUDGETS = {
    "persistence.1000.elapsed_ms_per_call": {"maximum": 120.0, "ownerPackage": 46},
    "persistence.5000.elapsed_ms_per_call": {"maximum": 300.0, "ownerPackage": 46},
    "persistence.5000.bytes_per_call": {"maximum": 64_000, "ownerPackage": 46},
    "rollback.5000.capture_ms": {"maximum": 0.5, "ownerPackage": 46},
    "transport.connections_per_turn": {"maximum": 1, "ownerPackage": 45},
    "transport.cancel_to_socket_close_ms": {"maximum": 100.0, "ownerPackage": 45},
    "import.tess_core_ms": {"maximum": 400.0, "ownerPackage": 47},
    "economics.v2_bytes_ratio": {"maximum": 0.20, "ownerPackage": 46},
    "economics.v2_recovery_cost_ratio": {"maximum": 4.0, "ownerPackage": 46},
    "economics.v2_fast_path_cpu_ratio": {"maximum": 0.60, "ownerPackage": 46},
}


def runPerformanceLane(*, inject_regression: bool = False, quick: bool = False) -> dict[str, Any]:
    samples = 1 if quick else 3
    persistence = runPersistenceBenchmark(
        message_counts=[1_000, 5_000],
        samples=samples,
        persists_per_sample=1,
        content_bytes=64 if quick else 1_536,
    )
    rollback = runRollbackSnapshotBenchmark(
        message_counts=[5_000],
        samples=5 if quick else 25,
        content_bytes=64 if quick else 1_536,
    )
    transport = runAnthropicTransportBenchmark(
        samples=1 if quick else 3,
        calls_per_turn=3,
        connection_setup_delay_ms=20 if quick else 80,
    )
    cache = runCacheEconomicLane()
    economics = runPersistenceEconomics(
        message_count=5_000,
        # Recovery economics are dominated by parser shape; retain the qualified
        # 1.5 KiB record class and five-sample median even in the quick lane.
        # Sparse fsync samples are too noisy for a hard relative-latency gate.
        content_bytes=1_536,
        samples=5,
    )
    measurements = _measurements(persistence, rollback, transport, economics)
    if inject_regression:
        measurements = {key: value * 2 for key, value in measurements.items()}
    regressions = _evaluate(measurements)
    regressions.extend(cache["regressions"])
    functional_gates = _functional_gates(persistence, economics)
    regressions.extend(
        {
            "metric": f"persistence.{gate['messageCount']}.{check}",
            "measured": False,
            "budget": True,
            "ownerPackage": 46,
            "verdict": "performance.regressed",
        }
        for gate in functional_gates
        for check, passed in gate["checks"].items()
        if not passed
    )
    return {
        "schemaVersion": LANE_SCHEMA,
        "status": "failed" if regressions else "passed",
        "runnerClass": _runner_class(persistence),
        "measurementPolicy": {
            "functional": "exact observed calls, physical writes, and bytes",
            "elapsed": "hard budgets apply to the recorded runner class without CPU normalization",
            "cache": "production Anthropic wire requests and parsed per-TTL usage over a deterministic loopback provider",
        },
        "injectedRegression": inject_regression,
        "budgets": BUDGETS,
        "measurements": measurements,
        "regressions": regressions,
        "functionalGates": functional_gates,
        "cacheEconomics": cache,
        "persistenceEconomics": economics,
    }


def writePerformanceLaneReport(report: dict[str, Any], output: str | Path) -> str:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def _measurements(
    persistence: dict[str, Any],
    rollback: dict[str, Any],
    transport: dict[str, Any],
    economics: dict[str, Any],
) -> dict[str, float]:
    series = {item["messageCount"]: item for item in persistence["series"]}
    rollback_row = rollback["series"][0]
    after = transport["beforeAfter"]["after"]
    cancel = transport["cancellation"]
    economics_comparison = economics["schemaV2Economics"]["comparison"]
    return {
        "persistence.1000.elapsed_ms_per_call": series[1_000]["median"]["wallElapsedNs"] / 1_000_000,
        "persistence.5000.elapsed_ms_per_call": series[5_000]["median"]["wallElapsedNs"] / 1_000_000,
        "persistence.5000.bytes_per_call": float(series[5_000]["median"]["storageBytes"]),
        "rollback.5000.capture_ms": rollback_row["rollbackCaptureMedianNs"] / 1_000_000,
        "transport.connections_per_turn": float(after["connectionCountMedian"]),
        "transport.cancel_to_socket_close_ms": float(cancel["after"]["cancelToSettlementMs"]),
        "import.tess_core_ms": _import_time_ms(),
        "economics.v2_bytes_ratio": 1.0 - float(economics_comparison["byteReductionRatio"]),
        "economics.v2_recovery_cost_ratio": float(economics_comparison["recoveryCostRatio"]),
        "economics.v2_fast_path_cpu_ratio": float(
            economics_comparison["fastPathVsHydratedCpuRatio"]
        ),
    }


def _functional_gates(
    persistence: dict[str, Any], economics: dict[str, Any]
) -> list[dict[str, Any]]:
    gates = []
    for row in persistence["series"]:
        median = row["median"]
        checks = {
            "persistCallCount": median["persistCallCount"] == 1,
            "persistFinishedCount": median["persistFinishedCount"] == 1,
            "storageWriteCount": median["storageWriteCount"] == 1,
            "storageAppendCount": median["storageAppendCount"] == 0,
            "storageBytesExact": median["storageBytes"] == row["serializedSessionBytesMedian"],
        }
        gates.append(
            {
                "messageCount": row["messageCount"],
                "status": "passed" if all(checks.values()) else "failed",
                "checks": checks,
            }
        )
    economics_checks = dict(economics["decisionGates"])
    gates.append(
        {
            "messageCount": economics["parameters"]["messageCount"],
            "scope": "representative-and-production-v2",
            "status": "passed" if all(economics_checks.values()) else "failed",
            "checks": economics_checks,
        }
    )
    return gates


def _evaluate(measurements: dict[str, float]) -> list[dict[str, Any]]:
    regressions = []
    for metric, budget in BUDGETS.items():
        measured = measurements[metric]
        if measured > budget["maximum"]:
            regressions.append(
                {
                    "metric": metric,
                    "measured": round(measured, 6),
                    "budget": budget["maximum"],
                    "ownerPackage": budget["ownerPackage"],
                    "verdict": "performance.regressed",
                }
            )
    return regressions


def _import_time_ms() -> float:
    started = time.perf_counter_ns()
    completed = subprocess.run(
        [sys.executable, "-c", "import tess_core"],
        capture_output=True,
        check=False,
        text=True,
    )
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if completed.returncode != 0:
        raise RuntimeError(f"tess_core import probe failed: {completed.stderr.strip()}")
    return round(elapsed, 3)


def _runner_class(report: dict[str, Any]) -> dict[str, Any]:
    environment = report["environment"]
    return {
        "implementation": environment["implementation"],
        "python": environment["python"],
        "platform": environment["platform"],
        "machine": environment["machine"],
    }


run_performance_lane = runPerformanceLane
