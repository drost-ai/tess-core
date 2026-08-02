from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

import agent_core as core
from agent_core.persistence_metrics import registerPersistenceObserver

from .performance import (
    REPORT_SCHEMA,
    PersistenceMeasurementCollector,
    _synthetic_session,
)
from .performance_support import environment as _environment
from .runtime_bindings import bind_agent_runtime


PERSISTENCE_ECONOMICS_BENCHMARK = "representative_iteration_and_production_v2"


async def runPersistenceEconomicsAsync(
    *,
    message_count: int = 5_000,
    stable_watermark: int | None = None,
    content_bytes: int = 1_536,
    samples: int = 3,
) -> dict[str, Any]:
    """Measure a representative turn and production v1/v2 persistence paths."""

    if message_count < 4 or content_bytes < 1 or samples < 1:
        raise ValueError("message_count >= 4 and positive content_bytes/samples are required")
    watermark = message_count - 2 if stable_watermark is None else stable_watermark
    if watermark < 1 or watermark >= message_count:
        raise ValueError("stable_watermark must leave at least one appended message")
    representative = await _measure_representative_persistence_iteration()
    production = await _measure_v1_v2_production(
        message_count=message_count,
        stable_watermark=watermark,
        content_bytes=content_bytes,
        samples=samples,
    )
    gates = {
        "representativeIncludesScopedAndUnscopedIo": (
            representative["persistCallCount"] > 0
            and representative["unscopedStorageWriteCount"] > 0
            and representative["unscopedStorageAppendCount"] > 0
        ),
        "failureRecoveryRecorded": representative["transcriptKinds"]
        == ["provider_failure", "provider_recovered"],
        "representativeReloadExact": representative["reloadExact"],
        "v2ByteReductionAtLeast80Percent": production["comparison"]["byteReductionRatio"] >= 0.80,
        "v2RecoveryWithinFourTimesV1": production["comparison"]["recoveryCostRatio"] <= 4.0,
        "v2CrashBoundaryFailsClosed": production["crashBoundary"]["passed"],
        "v2WriteBackExact": production["writeBack"]["exact"],
    }
    return {
        "schemaVersion": REPORT_SCHEMA,
        "benchmark": PERSISTENCE_ECONOMICS_BENCHMARK,
        "measurementPolicy": {
            "representative": "two real runAgentPrompt turns: provider failure, then recovery and successful commit; every storage_io event is counted",
            "comparison": "same session and runner, legacy full-record write versus the production v2 transaction and loader",
            "schemaStatus": "production session schema v2",
        },
        "environment": _environment(),
        "parameters": {
            "messageCount": message_count,
            "stableWatermark": watermark,
            "appendedMessages": message_count - watermark,
            "contentBytesPerMessage": content_bytes,
            "samples": samples,
        },
        "representativeIteration": representative,
        "schemaV2Economics": production,
        "decisionGates": gates,
        "status": "passed" if all(gates.values()) else "failed",
    }


def runPersistenceEconomics(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(runPersistenceEconomicsAsync(**kwargs))


async def _measure_representative_persistence_iteration() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agent-core-representative-persist-") as tmp:
        root = Path(tmp)
        home, workspace = root / "home", root / "workspace"
        home.mkdir()
        workspace.mkdir()
        async with core.scopedRuntimeHome(str(home)):
            session = core.createEmptySession(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                systemPrompt="Representative persistence iteration.",
                permissionMode="bypassPermissions",
                sessionId="representative-persistence",
            )
            profile = {
                "name": "representative",
                "kind": "openai",
                "apiMode": "responses",
                "maxContextLength": 100_000,
                "maxOutputTokens": 4_096,
            }
            config = _representative_config(workspace, home, profile)
            runtime = bind_agent_runtime(session)
            failure = core.createProviderRequestError(
                core.createProviderFailureRecord(
                    provider="openai",
                    providerLabel="OpenAI",
                    phase="request_start",
                    category="transport",
                    retryable=True,
                    attempt=1,
                    hadAssistantOutput=False,
                    hadToolUse=False,
                    rawMessage="representative injected connection reset",
                )
            )

            async def fail(_request: dict[str, Any]) -> dict[str, Any]:
                raise failure

            async def recover(_request: dict[str, Any]) -> dict[str, Any]:
                return {
                    "model": "gpt-5.4",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 8, "output_tokens": 2},
                    "content": [{"type": "text", "text": "recovered"}],
                }

            collector = PersistenceMeasurementCollector()
            unsubscribe = registerPersistenceObserver(collector.observe)
            started = time.perf_counter_ns()
            try:
                try:
                    await core.runAgentPrompt(
                        session=session,
                        config=config,
                        runtime=runtime,
                        prompt="first attempt",
                        providerOverride={"send": fail},
                    )
                except core.ProviderRequestError:
                    pass
                else:
                    raise RuntimeError("Representative failure turn unexpectedly succeeded")
                await core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=runtime,
                    prompt="resume after provider failure",
                    providerOverride={"send": recover},
                )
            finally:
                elapsed = time.perf_counter_ns() - started
                unsubscribe()
            return await _representative_summary(collector, elapsed, session)


def _representative_config(
    workspace: Path, home: Path, profile: dict[str, Any]
) -> dict[str, Any]:
    return {
        "cwd": str(workspace),
        "runtimeHome": str(home),
        "allowedRoots": [str(workspace)],
        "agents": {},
        "provider": "openai",
        "providerKind": "openai",
        "providerProfileName": "representative",
        "providerProfile": profile,
        "providerProfiles": {"representative": profile},
        "customHeaders": {},
        "openaiApiMode": "responses",
        "model": "gpt-5.4",
        "effort": "medium",
        "assistantName": "Harness",
        "assistantDescription": "",
        "systemPrompt": "Representative persistence iteration.",
        "tools": [],
        "allowedTools": [],
        "disallowedTools": [],
        "permissionMode": "bypassPermissions",
        "sessionPersistence": True,
    }


async def _representative_summary(
    collector: PersistenceMeasurementCollector,
    elapsed: int,
    session: dict[str, Any],
) -> dict[str, Any]:
    summary = collector.summary(elapsed)
    restored = await core.loadSession(session["id"])
    log_path = Path(core.sessionTranscriptLogPath(session["id"]))
    entries = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary.update(
        {
            "transcriptKinds": [
                entry["type"]
                for entry in entries
                if entry.get("type") in {"provider_failure", "provider_recovered"}
            ],
            "reloadExact": isinstance(restored, dict)
            and restored.get("messages") == session.get("messages"),
            "finalMessageCount": len(session.get("messages") or []),
            "ioEvents": [
                {
                    key: event.get(key)
                    for key in ("operation", "persistenceCallId", "byteCount", "filePath")
                }
                for event in collector.events
                if event["event"] == "storage_io"
            ],
        }
    )
    return summary


async def _measure_v1_v2_production(
    *, message_count: int, stable_watermark: int, content_bytes: int, samples: int
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agent-core-v2-production-") as tmp:
        root = Path(tmp)
        timings = {key: [] for key in ("v1WriteNs", "v2WriteNs", "v1RecoveryNs", "v2RecoveryNs")}
        measurements = {"v1Bytes": [], "v2Bytes": [], "v1Writes": [], "v2Writes": [], "v2Appends": []}
        for sample in range(samples):
            session = _synthetic_session(root, message_count, content_bytes, sample)
            base = copy.deepcopy(session)
            base["messages"] = base["messages"][:stable_watermark]
            expected = copy.deepcopy(session)
            expected["revision"] = 1
            v1_home = root / f"v1-{sample}"
            async with core.scopedRuntimeHome(str(v1_home)):
                await core.writeJsonFile(core.sessionPath(session["id"]), base)
                observed = await _observe_storage_io(
                    core.writeJsonFile(core.sessionPath(session["id"]), expected)
                )
                _record_observation(observed, measurements, timings, "v1")
                started = time.perf_counter_ns()
                recovered_v1 = await core.readJsonFile(core.sessionPath(session["id"]), None)
                timings["v1RecoveryNs"].append(time.perf_counter_ns() - started)
            v2_home = root / f"v2-{sample}"
            async with core.scopedRuntimeHome(str(v2_home)):
                await core.saveSession(base)

                async def commit(_latest: dict[str, Any] | None) -> dict[str, Any]:
                    return {"session": session, "changed": True}

                observed = await _observe_storage_io(
                    core.mutateSessionRecordTransaction(session["id"], commit)
                )
                _record_observation(observed, measurements, timings, "v2")
                started = time.perf_counter_ns()
                recovered_v2 = await core.loadSession(session["id"])
                timings["v2RecoveryNs"].append(time.perf_counter_ns() - started)
            if recovered_v1 != expected or recovered_v2 != expected:
                raise RuntimeError("Production v1/v2 recovery diverged from the source session")
        crash = await _production_crash_boundary(root, message_count, content_bytes, stable_watermark)
        write_back = await _production_write_back(root, message_count, content_bytes)
        return _production_report(stable_watermark, message_count, timings, measurements, crash, write_back)


async def _observe_storage_io(operation: Any) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    unsubscribe = registerPersistenceObserver(events.append)
    started = time.perf_counter_ns()
    try:
        result = await operation
    finally:
        elapsed = time.perf_counter_ns() - started
        unsubscribe()
    io = [event for event in events if event.get("event") == "storage_io"]
    return {"result": result, "elapsedNs": elapsed, "events": io}


def _record_observation(
    observed: dict[str, Any],
    measurements: dict[str, list[int]],
    timings: dict[str, list[int]],
    version: str,
) -> None:
    events = observed["events"]
    timings[f"{version}WriteNs"].append(observed["elapsedNs"])
    measurements[f"{version}Bytes"].append(sum(int(event["byteCount"]) for event in events))
    measurements[f"{version}Writes"].append(sum(event["operation"] == "write" for event in events))
    if version == "v2":
        measurements["v2Appends"].append(sum(event["operation"] == "append" for event in events))


async def _production_crash_boundary(
    root: Path, message_count: int, content_bytes: int, watermark: int
) -> dict[str, Any]:
    home = root / "crash"
    session = _synthetic_session(root, message_count, content_bytes, 91)
    base = copy.deepcopy(session)
    base["messages"] = base["messages"][:watermark]

    class InjectedCrash(RuntimeError):
        pass

    async with core.scopedRuntimeHome(str(home)):
        await core.saveSession(base)

        async def commit(_latest: dict[str, Any] | None) -> dict[str, Any]:
            return {"session": session, "changed": True}

        def inject(boundary: str) -> None:
            if boundary == "after_body":
                raise InjectedCrash(boundary)

        try:
            await core.mutateSessionRecordTransaction(session["id"], commit, faultInjector=inject)
        except InjectedCrash:
            pass
        restored = await core.loadSession(session["id"])
        head = await core.readJsonFile(core.sessionPath(session["id"]), None)
    return {
        "injected": "production commit interrupted after durable body append and before head replace",
        "committedMessages": len(restored["messages"]),
        "expectedCommittedMessages": watermark,
        "headMessageCount": head["messageCount"],
        "passed": restored["messages"] == base["messages"] and head["messageCount"] == watermark,
    }


async def _production_write_back(root: Path, message_count: int, content_bytes: int) -> dict[str, Any]:
    home = root / "write-back"
    session = _synthetic_session(root, message_count, content_bytes, 92)
    async with core.scopedRuntimeHome(str(home)):
        await core.writeJsonFile(core.sessionPath(session["id"]), session)
        before = Path(core.sessionPath(session["id"])).read_bytes()
        await core.migrateSessionToV2(session["id"])
        await core.writeBackSessionV1(session["id"])
        after = Path(core.sessionPath(session["id"])).read_bytes()
    return {
        "beforeDigest": hashlib.sha256(before).hexdigest(),
        "afterDigest": hashlib.sha256(after).hexdigest(),
        "exact": before == after,
    }


def _production_report(
    stable_watermark: int,
    message_count: int,
    timings: dict[str, list[int]],
    measurements: dict[str, list[int]],
    crash: dict[str, Any],
    write_back: dict[str, Any],
) -> dict[str, Any]:
    medians = {key: int(statistics.median(value)) for key, value in timings.items()}
    values = {key: int(statistics.median(value)) for key, value in measurements.items()}
    v1_bytes, v2_bytes = values["v1Bytes"], values["v2Bytes"]
    return {
        "v1": {
            "bytesPerCommit": v1_bytes,
            "physicalWriteCount": values["v1Writes"],
            "writeMedianNs": medians["v1WriteNs"],
            "recoveryMedianNs": medians["v1RecoveryNs"],
        },
        "v2Production": {
            "bytesPerCommit": v2_bytes,
            "physicalWriteCount": values["v2Writes"] + values["v2Appends"],
            "headWriteCount": values["v2Writes"],
            "bodyAppendCount": values["v2Appends"],
            "writeMedianNs": medians["v2WriteNs"],
            "recoveryMedianNs": medians["v2RecoveryNs"],
            "baseMessageCount": stable_watermark,
            "appendedMessageCount": message_count - stable_watermark,
        },
        "comparison": {
            "byteReductionRatio": round(1 - (v2_bytes / max(1, v1_bytes)), 6),
            "writeLatencyRatio": round(medians["v2WriteNs"] / max(1, medians["v1WriteNs"]), 6),
            "recoveryCostRatio": round(medians["v2RecoveryNs"] / max(1, medians["v1RecoveryNs"]), 6),
            "writeCountDelta": 1,
        },
        "crashBoundary": crash,
        "writeBack": write_back,
        "samples": {**timings, **measurements},
    }


run_persistence_economics = runPersistenceEconomics
