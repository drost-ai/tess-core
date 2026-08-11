from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import statistics
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import agent_core._facade as core
from agent_core.storage.persistence_metrics import register_persistence_observer
from agent_core.providers.turn_session import create_provider_turn_session
from agent_core.providers.watchdog import AbortSignal
from agent_core.agent.cancellation import _capture_session_rollback

from .fake_providers.server import FakeProviderHttpServer
from .performance_support import (
    environment as _environment,
    median_sample as _median_sample,
    validate_sample as _validate_sample,
    validated_counts as _validated_counts,
)

REPORT_SCHEMA = "agent-core.performance.v1"
PERSISTENCE_BENCHMARK = "persist_session_steady_state"
ANTHROPIC_TRANSPORT_BENCHMARK = "anthropic_turn_transport"
ROLLBACK_SNAPSHOT_BENCHMARK = "session_rollback_snapshot"
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
        all_io_events = [event for event in self.events if event["event"] == "storage_io"]
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
            "allStorageWriteCount": sum(event["operation"] == "write" for event in all_io_events),
            "allStorageAppendCount": sum(event["operation"] == "append" for event in all_io_events),
            "allStorageBytes": sum(int(event["byteCount"]) for event in all_io_events),
            "unscopedStorageWriteCount": sum(
                event["operation"] == "write" and event.get("persistenceCallId") is None
                for event in all_io_events
            ),
            "unscopedStorageAppendCount": sum(
                event["operation"] == "append" and event.get("persistenceCallId") is None
                for event in all_io_events
            ),
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
        async with core.scoped_runtime_home(str(runtime_home)):
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
def runRollbackSnapshotBenchmark(
    *,
    message_counts: Iterable[int] = (200, 1_000, 5_000),
    samples: int = 25,
    content_bytes: int = 1_536,
) -> dict[str, Any]:
    counts = _validated_counts(message_counts)
    if samples < 1 or content_bytes < 1:
        raise ValueError("samples and content_bytes must be positive")
    calibration = runCpuCalibration()
    with tempfile.TemporaryDirectory(prefix="agent-core-rollback-performance-") as tmp:
        workspace = Path(tmp)
        series = [
            _measure_rollback_snapshot_count(
                workspace,
                message_count,
                samples,
                content_bytes,
            )
            for message_count in counts
        ]
    return {
        "schemaVersion": REPORT_SCHEMA,
        "benchmark": ROLLBACK_SNAPSHOT_BENCHMARK,
        "measurementPolicy": {
            "legacy": "deepcopy whole session; Package 46 pre-S2a behavior",
            "after": "one owned turn-start copy, then deepcopy non-message head plus appended message suffix",
            "elapsedMetrics": "informational before/after evidence; functional equivalence is hard-gated in tests",
        },
        "environment": _environment(),
        "calibration": calibration,
        "parameters": {
            "messageCounts": counts,
            "samplesPerMessageCount": samples,
            "contentBytesPerMessage": content_bytes,
        },
        "series": series,
    }
async def runAnthropicTransportBenchmarkAsync(
    *,
    samples: int = 3,
    calls_per_turn: int = 3,
    connection_setup_delay_ms: int = 80,
) -> dict[str, Any]:
    if samples < 1 or calls_per_turn < 2 or connection_setup_delay_ms < 1:
        raise ValueError("samples and setup delay must be positive; calls_per_turn must be at least 2")
    modes: dict[str, list[dict[str, Any]]] = {"capability_off": [], "capability_on": []}
    for enabled, name in ((False, "capability_off"), (True, "capability_on")):
        for _ in range(samples):
            modes[name].append(
                await _measure_anthropic_transport_turn(
                    enabled=enabled,
                    calls_per_turn=calls_per_turn,
                    setup_delay_ms=connection_setup_delay_ms,
                )
            )
    cancel = await _measure_anthropic_cancel()
    summaries = {
        name: {
            "elapsedMsMedian": round(statistics.median(item["elapsedMs"] for item in items), 3),
            "connectionCountMedian": int(statistics.median(item["connectionCount"] for item in items)),
            "reusedRequestsMedian": int(statistics.median(item["reusedRequests"] for item in items)),
            "samples": items,
        }
        for name, items in modes.items()
    }
    before_connections = summaries["capability_off"]["connectionCountMedian"]
    after_connections = summaries["capability_on"]["connectionCountMedian"]
    return {
        "schemaVersion": REPORT_SCHEMA,
        "benchmark": ANTHROPIC_TRANSPORT_BENCHMARK,
        "environment": _environment(),
        "parameters": {
            "samplesPerMode": samples,
            "callsPerTurn": calls_per_turn,
            "modeledConnectionSetupDelayMs": connection_setup_delay_ms,
            "endpoint": "offline-loopback-fake-provider",
        },
        "beforeAfter": {
            "before": summaries["capability_off"],
            "after": summaries["capability_on"],
            "connectionsAvoidedPerTurn": before_connections - after_connections,
            "modeledSetupLatencyAvoidedMs": (
                before_connections - after_connections
            ) * connection_setup_delay_ms,
        },
        "cancellation": cancel,
    }


def runAnthropicTransportBenchmark(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(runAnthropicTransportBenchmarkAsync(**kwargs))


def writePerformanceReport(report: dict[str, Any], output: str) -> str:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def _measure_rollback_snapshot_count(
    workspace: Path,
    message_count: int,
    samples: int,
    content_bytes: int,
) -> dict[str, Any]:
    session = _synthetic_session(workspace, message_count, content_bytes, 0)
    cold_started = time.perf_counter_ns()
    authority = _capture_session_rollback(session)
    cold_capture_ns = time.perf_counter_ns() - cold_started
    legacy_ns: list[int] = []
    rollback_ns: list[int] = []
    rollback_head_bytes: list[int] = []
    for _ in range(samples):
        session["messages"].append({"role": "user", "content": "incremental-boundary"})
        started = time.perf_counter_ns()
        legacy = copy.deepcopy(session)
        legacy_ns.append(time.perf_counter_ns() - started)

        started = time.perf_counter_ns()
        rollback = _capture_session_rollback(session, authority)
        rollback_ns.append(time.perf_counter_ns() - started)
        authority = rollback
        rollback_head_bytes.append(
            len(json.dumps(rollback.head, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        )
        if len(legacy["messages"]) != rollback.messageWatermark:
            raise RuntimeError("Rollback watermark diverged from the legacy snapshot")
        if rollback.frozenMessages[-1] is session["messages"][-1]:
            raise RuntimeError("Rollback capture retained mutable live message authority")
    legacy_median = int(statistics.median(legacy_ns))
    rollback_median = int(statistics.median(rollback_ns))
    return {
        "messageCount": message_count,
        "legacyDeepcopyMedianNs": legacy_median,
        "rollbackCaptureMedianNs": rollback_median,
        "rollbackHeadBytesMedian": int(statistics.median(rollback_head_bytes)),
        "turnStartColdCaptureNs": cold_capture_ns,
        "speedupRatio": round(legacy_median / max(1, rollback_median), 3),
        "legacySamplesNs": legacy_ns,
        "rollbackSamplesNs": rollback_ns,
    }


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
        await core.persist_session(session, {"sessionPersistence": True}, touch=False)
        collector = PersistenceMeasurementCollector()
        unsubscribe = register_persistence_observer(collector.observe)
        started_ns = time.perf_counter_ns()
        try:
            for invocation in range(persists_per_sample):
                session["metadata"]["benchmarkSequence"] = invocation + 1
                await core.persist_session(session, {"sessionPersistence": True}, touch=False)
        finally:
            wall_elapsed_ns = time.perf_counter_ns() - started_ns
            unsubscribe()
        result = collector.summary(wall_elapsed_ns)
        _validate_sample(result, persists_per_sample)
        results.append(result)
        serialized_bytes.append(os.path.getsize(core.session_path(session["id"])))
    return {
        "messageCount": message_count,
        "serializedSessionBytesMedian": int(statistics.median(serialized_bytes)),
        "samples": results,
        "median": _median_sample(results),
    }


async def _measure_anthropic_transport_turn(
    *, enabled: bool, calls_per_turn: int, setup_delay_ms: int
) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    server = FakeProviderHttpServer(
        provider="anthropic",
        script={"connectionSetupDelayMs": setup_delay_ms},
        request_recorder=requests,
    ).start()
    runtime = {"emitEvent": lambda *_: None}
    config = _benchmark_anthropic_config(server, enabled)
    turn = create_provider_turn_session(
        logical_turn_id="package45-benchmark",
        session={"id": "package45-benchmark", "provider": "anthropic"},
        config=config,
        runtime=runtime,
        model="claude-sonnet-4-6",
    )
    started = time.perf_counter()
    try:
        for _ in range(calls_per_turn):
            await core.send_anthropic_messages_request(
                config=config,
                request=_benchmark_anthropic_request(),
                runtime=runtime,
                maxAttempts=1,
                turnSession=turn,
            )
        elapsed_ms = (time.perf_counter() - started) * 1_000
        telemetry = turn.transport_telemetry()
    finally:
        await turn.close()
        server.close()
    return {
        "elapsedMs": round(elapsed_ms, 3),
        "connectionCount": len({item["connectionId"] for item in requests}),
        "requestCount": len(requests),
        "reusedRequests": telemetry["reusedRequests"],
        "liveThreadsAfterClose": telemetry.get("threadsStarted", 0) - telemetry.get("threadsSettled", 0),
    }


async def _measure_anthropic_cancel() -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    server = FakeProviderHttpServer(
        provider="anthropic",
        script={"messages": [{"bodyDelayMs": 500}]},
        request_recorder=requests,
    ).start()
    signal = AbortSignal()
    runtime = {"abortSignal": signal, "streamProviderMessages": True, "emitEvent": lambda *_: None}
    config = _benchmark_anthropic_config(server, False)
    turn = create_provider_turn_session(
        logical_turn_id="package45-cancel",
        session={"id": "package45-cancel", "provider": "anthropic"},
        config=config,
        runtime=runtime,
        model="claude-sonnet-4-6",
    )
    abort_at = 0.0

    async def abort_after_headers() -> None:
        nonlocal abort_at
        while not requests:
            await asyncio.sleep(0)
        await asyncio.sleep(0.005)
        abort_at = time.perf_counter()
        signal.abort()

    abort_task = asyncio.create_task(abort_after_headers())
    category = None
    try:
        await core.send_anthropic_messages_request(
            config=config,
            request=_benchmark_anthropic_request(),
            runtime=runtime,
            maxAttempts=1,
            streamIdleTimeoutMs=1_000,
            turnSession=turn,
        )
    except core.ProviderRequestError as exc:
        category = exc.record.get("category")
    settled_at = time.perf_counter()
    await abort_task
    await turn.close()
    for _ in range(20):
        if requests and requests[0].get("clientDisconnected"):
            break
        await asyncio.sleep(0.005)
    telemetry = turn.transport_telemetry()
    server.close()
    return {
        "baseline": {
            "abortPollIntervalMs": 50,
            "socketCloseOnCancel": False,
            "workerThreadsTracked": False,
        },
        "after": {
            "cancelToSettlementMs": round((settled_at - abort_at) * 1_000, 3),
            "serverObservedDisconnect": bool(requests and requests[0].get("clientDisconnected")),
            "failureCategory": category,
            "liveThreadsAfterClose": telemetry.get("threadsStarted", 0) - telemetry.get("threadsSettled", 0),
        },
    }


def _benchmark_anthropic_config(
    server: FakeProviderHttpServer, enabled: bool
) -> dict[str, Any]:
    return {
        "provider": "anthropic",
        "anthropicBaseUrl": server.base_url,
        "apiKey": "package45-test-key",
        "customHeaders": {},
        "providerCapabilities": {"anthropicHttpKeepAlive": enabled},
    }


def _benchmark_anthropic_request() -> dict[str, Any]:
    return {
        "model": "claude-sonnet-4-6",
        "system": "Package 45 benchmark.",
        "messages": [{"role": "user", "content": "measure transport"}],
        "tools": [],
    }


def _synthetic_session(
    workspace: Path,
    message_count: int,
    content_bytes: int,
    sample_index: int,
) -> dict[str, Any]:
    session = core.create_empty_session(
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
