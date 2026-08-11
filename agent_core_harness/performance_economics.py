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

import agent_core._facade as core
from agent_core.storage.persistence_metrics import register_persistence_observer

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
        "v2FastPathCutsHydratedCpuAtLeast40Percent": (
            production["comparison"]["fastPathVsHydratedCpuRatio"] <= 0.60
        ),
        "v2CrashBoundaryFailsClosed": production["crashBoundary"]["passed"],
        "v2WriteBackExact": production["writeBack"]["exact"],
    }
    return {
        "schemaVersion": REPORT_SCHEMA,
        "benchmark": PERSISTENCE_ECONOMICS_BENCHMARK,
        "measurementPolicy": {
            "representative": "two real runAgentPrompt turns: provider failure, then recovery and successful commit; every storage_io event is counted",
            "comparison": "same session and runner: legacy full-record write, authority-free hydrated v2 fallback, and the production append-watermark path; recovery measures the persisted head/body loader without transcript reconciliation",
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
        async with core.scoped_runtime_home(str(home)):
            session = core.create_empty_session(
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
            unsubscribe = register_persistence_observer(collector.observe)
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
    restored = await core.load_session(session["id"])
    log_path = Path(core.session_transcript_log_path(session["id"]))
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
        timing_names = (
            "v1WriteNs", "v1WriteCpuNs", "v1RecoveryNs", "v2HydratedWriteNs", "v2HydratedWriteCpuNs", "v2HydratedRecoveryNs", "v2FastWriteNs", "v2FastWriteCpuNs", "v2FastRecoveryNs",
        )
        measurement_names = (
            "v1Bytes", "v1Writes", "v2HydratedBytes", "v2HydratedWrites", "v2HydratedAppends", "v2FastBytes", "v2FastWrites", "v2FastAppends",
        )
        timings = {key: [] for key in timing_names}
        measurements = {key: [] for key in measurement_names}
        for sample in range(samples):
            session = _synthetic_session(root, message_count, content_bytes, sample)
            base = copy.deepcopy(session)
            base["messages"] = base["messages"][:stable_watermark]
            expected = copy.deepcopy(session)
            expected["revision"] = 1
            v1_home = root / f"v1-{sample}"
            async with core.scoped_runtime_home(str(v1_home)):
                await core.write_json_file(core.session_path(session["id"]), base)
                observed = await _observe_storage_io(
                    core.write_json_file(core.session_path(session["id"]), expected)
                )
                _record_observation(observed, measurements, timings, "v1")
                started = time.perf_counter_ns()
                recovered_v1 = await core.read_json_file(core.session_path(session["id"]), None)
                timings["v1RecoveryNs"].append(time.perf_counter_ns() - started)
            hydrated_home = root / f"v2-hydrated-{sample}"
            async with core.scoped_runtime_home(str(hydrated_home)):
                await core.save_session(base)

                async def commit(_latest: dict[str, Any] | None) -> dict[str, Any]:
                    return {"session": session, "changed": True}

                observed = await _observe_storage_io(
                    core.mutate_session_record_transaction(session["id"], commit)
                )
                _record_observation(
                    observed, measurements, timings, "v2Hydrated"
                )
                started = time.perf_counter_ns()
                hydrated_head = await core.read_json_file(
                    core.session_path(session["id"]), None
                )
                recovered_hydrated = (
                    await core.hydrate_persisted_session(
                        session["id"], hydrated_head, repairHead=False
                    )
                )["record"]
                timings["v2HydratedRecoveryNs"].append(
                    time.perf_counter_ns() - started
                )
            fast_home = root / f"v2-fast-{sample}"
            async with core.scoped_runtime_home(str(fast_home)):
                fast_session = copy.deepcopy(base)
                await core.persistSession(
                    fast_session, {"sessionPersistence": True}, touch=False
                )
                fast_session["messages"].extend(
                    copy.deepcopy(session["messages"][stable_watermark:])
                )
                observed = await _observe_storage_io(
                    core.persistSession(
                        fast_session,
                        {"sessionPersistence": True},
                        touch=False,
                    )
                )
                _record_observation(observed, measurements, timings, "v2Fast")
                started = time.perf_counter_ns()
                fast_head = await core.read_json_file(
                    core.session_path(session["id"]), None
                )
                recovered_fast = (
                    await core.hydrate_persisted_session(
                        session["id"], fast_head, repairHead=False
                    )
                )["record"]
                timings["v2FastRecoveryNs"].append(
                    time.perf_counter_ns() - started
                )
            if recovered_v1 != expected or recovered_hydrated != expected:
                raise RuntimeError("Production v1/v2 recovery diverged from the source session")
            if recovered_fast is None or recovered_fast["messages"] != session["messages"]:
                raise RuntimeError("Production v2 fast-path recovery diverged from the source messages")
        crash = await _production_crash_boundary(root, message_count, content_bytes, stable_watermark)
        write_back = await _production_write_back(root, message_count, content_bytes)
        return _production_report(stable_watermark, message_count, timings, measurements, crash, write_back)


async def _observe_storage_io(operation: Any) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    unsubscribe = register_persistence_observer(events.append)
    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    try:
        result = await operation
    finally:
        cpu_elapsed = time.process_time_ns() - cpu_started
        elapsed = time.perf_counter_ns() - started
        unsubscribe()
    io = [event for event in events if event.get("event") == "storage_io"]
    return {
        "result": result,
        "elapsedNs": elapsed,
        "cpuNs": cpu_elapsed,
        "events": io,
    }


def _record_observation(
    observed: dict[str, Any],
    measurements: dict[str, list[int]],
    timings: dict[str, list[int]],
    version: str,
) -> None:
    events = observed["events"]
    timings[f"{version}WriteNs"].append(observed["elapsedNs"])
    timings[f"{version}WriteCpuNs"].append(observed["cpuNs"])
    measurements[f"{version}Bytes"].append(sum(int(event["byteCount"]) for event in events))
    measurements[f"{version}Writes"].append(sum(event["operation"] == "write" for event in events))
    if version.startswith("v2"):
        measurements[f"{version}Appends"].append(
            sum(event["operation"] == "append" for event in events)
        )


async def _production_crash_boundary(
    root: Path, message_count: int, content_bytes: int, watermark: int
) -> dict[str, Any]:
    home = root / "crash"
    session = _synthetic_session(root, message_count, content_bytes, 91)
    base = copy.deepcopy(session)
    base["messages"] = base["messages"][:watermark]

    class InjectedCrash(RuntimeError):
        pass

    async with core.scoped_runtime_home(str(home)):
        await core.persistSession(base, {"sessionPersistence": True}, touch=False)
        committed_messages = copy.deepcopy(base["messages"])
        base["messages"].extend(
            copy.deepcopy(session["messages"][watermark:])
        )

        def inject(boundary: str) -> None:
            if boundary == "after_body":
                raise InjectedCrash(boundary)

        try:
            await core.persistSession(
                base,
                {"sessionPersistence": True},
                touch=False,
                faultInjector=inject,
            )
        except InjectedCrash:
            pass
        head = await core.read_json_file(core.session_path(session["id"]), None)
        restored = (
            await core.hydrate_persisted_session(
                session["id"], head, repairHead=False
            )
        )["record"]
    return {
        "injected": "runtime fast path interrupted after durable body append and before head replace",
        "committedMessages": len(restored["messages"]),
        "expectedCommittedMessages": watermark,
        "headMessageCount": head["messageCount"],
        "passed": restored["messages"] == committed_messages and head["messageCount"] == watermark,
    }


async def _production_write_back(root: Path, message_count: int, content_bytes: int) -> dict[str, Any]:
    home = root / "write-back"
    session = _synthetic_session(root, message_count, content_bytes, 92)
    async with core.scoped_runtime_home(str(home)):
        await core.write_json_file(core.session_path(session["id"]), session)
        before = Path(core.session_path(session["id"])).read_bytes()
        await core.migrate_session_to_v2(session["id"])
        await core.write_back_session_v1(session["id"])
        after = Path(core.session_path(session["id"])).read_bytes()
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
    v1_bytes = values["v1Bytes"]
    hydrated_bytes = values["v2HydratedBytes"]
    fast_bytes = values["v2FastBytes"]
    return {
        "v1": {
            "bytesPerCommit": v1_bytes,
            "physicalWriteCount": values["v1Writes"],
            "writeMedianNs": medians["v1WriteNs"],
            "writeCpuMedianNs": medians["v1WriteCpuNs"],
            "recoveryMedianNs": medians["v1RecoveryNs"],
        },
        "v2Production": {
            "bytesPerCommit": fast_bytes,
            "physicalWriteCount": values["v2FastWrites"] + values["v2FastAppends"],
            "headWriteCount": values["v2FastWrites"],
            "bodyAppendCount": values["v2FastAppends"],
            "writeMedianNs": medians["v2FastWriteNs"],
            "writeCpuMedianNs": medians["v2FastWriteCpuNs"],
            "recoveryMedianNs": medians["v2FastRecoveryNs"],
            "baseMessageCount": stable_watermark,
            "appendedMessageCount": message_count - stable_watermark,
        },
        "v2HydratedFallback": {
            "bytesPerCommit": hydrated_bytes,
            "physicalWriteCount": values["v2HydratedWrites"] + values["v2HydratedAppends"],
            "headWriteCount": values["v2HydratedWrites"],
            "bodyAppendCount": values["v2HydratedAppends"],
            "writeMedianNs": medians["v2HydratedWriteNs"],
            "writeCpuMedianNs": medians["v2HydratedWriteCpuNs"],
            "recoveryMedianNs": medians["v2HydratedRecoveryNs"],
        },
        "comparison": {
            "byteReductionRatio": round(1 - (fast_bytes / max(1, v1_bytes)), 6),
            "writeLatencyRatio": round(medians["v2FastWriteNs"] / max(1, medians["v1WriteNs"]), 6),
            "recoveryCostRatio": round(medians["v2FastRecoveryNs"] / max(1, medians["v1RecoveryNs"]), 6),
            "fastPathVsHydratedLatencyRatio": round(
                medians["v2FastWriteNs"] / max(1, medians["v2HydratedWriteNs"]),
                6,
            ),
            "fastPathVsHydratedCpuRatio": round(
                medians["v2FastWriteCpuNs"]
                / max(1, medians["v2HydratedWriteCpuNs"]),
                6,
            ),
            "fastPathVsHydratedByteRatio": round(
                fast_bytes / max(1, hydrated_bytes),
                6,
            ),
            "writeCountDelta": values["v2FastWrites"] + values["v2FastAppends"] - values["v1Writes"],
        },
        "crashBoundary": crash,
        "writeBack": write_back,
        "samples": {**timings, **measurements},
    }


run_persistence_economics = runPersistenceEconomics
