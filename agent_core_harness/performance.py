from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

import agent_core as core
from agent_core.persistence_metrics import registerPersistenceObserver


REPORT_SCHEMA = "agent-core.performance.v1"
PERSISTENCE_BENCHMARK = "persist_session_steady_state"


class PersistenceMeasurementCollector:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def observe(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def summary(self, wall_elapsed_ns: int) -> dict[str, Any]:
        starts = [event for event in self.events if event["event"] == "persist_call_started"]
        finishes = [event for event in self.events if event["event"] == "persist_call_finished"]
        io_events = [
            event
            for event in self.events
            if event["event"] == "storage_io" and event.get("persistenceCallId") is not None
        ]
        outcomes: dict[str, int] = {}
        for event in finishes:
            outcome = str(event["outcome"])
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return {
            "persistCallCount": len(starts),
            "persistFinishedCount": len(finishes),
            "persistOutcomes": outcomes,
            "storageWriteCount": sum(event["operation"] == "write" for event in io_events),
            "storageAppendCount": sum(event["operation"] == "append" for event in io_events),
            "storageBytes": sum(int(event["byteCount"]) for event in io_events),
            "storageElapsedNs": sum(int(event["elapsedNs"]) for event in io_events),
            "persistenceElapsedNs": sum(int(event["elapsedNs"]) for event in finishes),
            "wallElapsedNs": wall_elapsed_ns,
        }


def runCpuCalibration(*, samples: int = 5, rounds: int = 10_000) -> dict[str, Any]:
    durations: list[int] = []
    final_digest = b"agent-core-package-47a"
    for sample in range(samples):
        digest = final_digest + bytes([sample])
        started_ns = time.perf_counter_ns()
        for _ in range(rounds):
            digest = hashlib.sha256(digest).digest()
        durations.append(time.perf_counter_ns() - started_ns)
        final_digest = digest
    return {
        "probe": "sha256-chain",
        "roundsPerSample": rounds,
        "sampleCount": samples,
        "sampleElapsedNs": durations,
        "medianElapsedNs": int(statistics.median(durations)),
        "digest": final_digest.hex(),
    }


async def runPersistenceBenchmarkAsync(
    *,
    message_counts: Iterable[int] = (200, 1_000, 5_000),
    samples: int = 5,
    persists_per_sample: int = 3,
    content_bytes: int = 1_536,
) -> dict[str, Any]:
    counts = _validated_counts(message_counts)
    if samples < 1 or persists_per_sample < 1 or content_bytes < 1:
        raise ValueError("samples, persists_per_sample, and content_bytes must be positive")
    calibration = runCpuCalibration()
    with tempfile.TemporaryDirectory(prefix="agent-core-performance-") as tmp:
        root = Path(tmp)
        runtime_home = root / "home"
        workspace = root / "workspace"
        runtime_home.mkdir()
        workspace.mkdir()
        async with core.scopedRuntimeHome(str(runtime_home)):
            series = [
                await _measure_message_count(
                    workspace,
                    message_count,
                    samples,
                    persists_per_sample,
                    content_bytes,
                )
                for message_count in counts
            ]
    return {
        "schemaVersion": REPORT_SCHEMA,
        "benchmark": PERSISTENCE_BENCHMARK,
        "measurementPolicy": {
            "functionalMetrics": "exact; suitable for hard regression gates",
            "elapsedMetrics": "informational baseline in 47A",
            "calibrationScope": "CPU calibration does not normalize filesystem latency",
        },
        "environment": _environment(),
        "calibration": calibration,
        "parameters": {
            "messageCounts": counts,
            "samplesPerMessageCount": samples,
            "persistInvocationsPerSample": persists_per_sample,
            "contentBytesPerMessage": content_bytes,
            "mutation": "metadata.benchmarkSequence; touch=false",
        },
        "series": series,
    }


def runPersistenceBenchmark(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(runPersistenceBenchmarkAsync(**kwargs))


def writePerformanceReport(report: dict[str, Any], output: str) -> str:
    import json

    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


async def _measure_message_count(
    workspace: Path,
    message_count: int,
    sample_count: int,
    persists_per_sample: int,
    content_bytes: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    serialized_bytes: list[int] = []
    for sample_index in range(sample_count):
        session = _synthetic_session(workspace, message_count, content_bytes, sample_index)
        await core.persistSession(session, {"sessionPersistence": True}, touch=False)
        collector = PersistenceMeasurementCollector()
        unsubscribe = registerPersistenceObserver(collector.observe)
        started_ns = time.perf_counter_ns()
        try:
            for invocation in range(persists_per_sample):
                session["metadata"]["benchmarkSequence"] = invocation + 1
                await core.persistSession(session, {"sessionPersistence": True}, touch=False)
        finally:
            wall_elapsed_ns = time.perf_counter_ns() - started_ns
            unsubscribe()
        result = collector.summary(wall_elapsed_ns)
        _validate_sample(result, persists_per_sample)
        results.append(result)
        serialized_bytes.append(os.path.getsize(core.sessionPath(session["id"])))
    return {
        "messageCount": message_count,
        "serializedSessionBytesMedian": int(statistics.median(serialized_bytes)),
        "samples": results,
        "median": _median_sample(results),
    }


def _synthetic_session(
    workspace: Path,
    message_count: int,
    content_bytes: int,
    sample_index: int,
) -> dict[str, Any]:
    session = core.createEmptySession(
        cwd=str(workspace),
        provider="anthropic",
        model="claude-sonnet-4-6",
        systemPrompt="Package 47A persistence measurement.",
        permissionMode="default",
        sessionId=f"p47a-{message_count}-{sample_index}",
    )
    body = "x" * content_bytes
    session["messages"] = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message-{index:05d}:" + body,
        }
        for index in range(message_count)
    ]
    return session


def _validate_sample(sample: dict[str, Any], expected_calls: int) -> None:
    if sample["persistCallCount"] != expected_calls:
        raise RuntimeError(f"Persistence observer saw {sample['persistCallCount']} calls; expected {expected_calls}")
    if sample["persistFinishedCount"] != expected_calls:
        raise RuntimeError("A measured persistence call did not finish")
    if sample["persistOutcomes"] != {"persisted": expected_calls}:
        raise RuntimeError(f"Unexpected persistence outcomes: {sample['persistOutcomes']}")
    if sample["storageWriteCount"] != expected_calls or sample["storageBytes"] <= 0:
        raise RuntimeError("Measured persistence calls did not produce the expected physical writes")


def _median_sample(samples: list[dict[str, Any]]) -> dict[str, int]:
    keys = (
        "persistCallCount",
        "persistFinishedCount",
        "storageWriteCount",
        "storageAppendCount",
        "storageBytes",
        "storageElapsedNs",
        "persistenceElapsedNs",
        "wallElapsedNs",
    )
    return {key: int(statistics.median([int(sample[key]) for sample in samples])) for key in keys}


def _validated_counts(values: Iterable[int]) -> list[int]:
    counts = [int(value) for value in values]
    if not counts or any(value < 1 for value in counts):
        raise ValueError("message_counts must contain positive integers")
    if len(set(counts)) != len(counts):
        raise ValueError("message_counts must not contain duplicates")
    return counts


def _environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpuCount": os.cpu_count(),
    }
