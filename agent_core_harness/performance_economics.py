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


PERSISTENCE_ECONOMICS_BENCHMARK = "representative_iteration_and_v2_prototype"


async def runPersistenceEconomicsAsync(
    *,
    message_count: int = 5_000,
    stable_watermark: int | None = None,
    content_bytes: int = 1_536,
    samples: int = 3,
) -> dict[str, Any]:
    """Measure the real persistence surface and a non-committing v2 prototype."""

    if message_count < 4 or content_bytes < 1 or samples < 1:
        raise ValueError("message_count >= 4 and positive content_bytes/samples are required")
    watermark = message_count - 2 if stable_watermark is None else stable_watermark
    if watermark < 1 or watermark >= message_count:
        raise ValueError("stable_watermark must leave at least one appended message")
    representative = await _measure_representative_persistence_iteration()
    prototype = _measure_v1_v2_prototype(
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
        "v2ByteReductionAtLeast80Percent": prototype["comparison"]["byteReductionRatio"] >= 0.80,
        "v2RecoveryWithinFourTimesV1": prototype["comparison"]["recoveryCostRatio"] <= 4.0,
        "v2CrashBoundaryFailsClosed": prototype["crashBoundary"]["passed"],
        "v2WriteBackExact": prototype["writeBack"]["exact"],
    }
    return {
        "schemaVersion": REPORT_SCHEMA,
        "benchmark": PERSISTENCE_ECONOMICS_BENCHMARK,
        "measurementPolicy": {
            "representative": "two real runAgentPrompt turns: provider failure, then recovery and successful commit; every storage_io event is counted",
            "prototype": "same session and runner, v1 full-record rewrite versus v2 slim head plus append-only message suffix",
            "schemaStatus": "measurement-only prototype; no persisted contract change",
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
        "schemaPrototypeEconomics": prototype,
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


def _measure_v1_v2_prototype(
    *, message_count: int, stable_watermark: int, content_bytes: int, samples: int
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agent-core-v2-prototype-") as tmp:
        root = Path(tmp)
        session = _synthetic_session(root, message_count, content_bytes, 47)
        messages = copy.deepcopy(session["messages"])
        full_bytes = _json_bytes(session)
        head = copy.deepcopy(session)
        head.pop("messages", None)
        head["messageStore"] = {
            "schemaVersion": 2,
            "format": "jsonl",
            "messageWatermark": message_count,
        }
        head_bytes = _json_bytes(head)
        base_lines = b"".join(
            _json_bytes(message) + b"\n" for message in messages[:stable_watermark]
        )
        suffix_lines = b"".join(
            _json_bytes(message) + b"\n" for message in messages[stable_watermark:]
        )
        timings = {key: [] for key in ("v1WriteNs", "v2WriteNs", "v1RecoveryNs", "v2RecoveryNs")}
        recovered_v2: dict[str, Any] | None = None
        for sample in range(samples):
            v1_path = root / f"v1-{sample}.json"
            message_path = root / f"v2-{sample}.messages.jsonl"
            head_path = root / f"v2-{sample}.head.json"
            message_path.write_bytes(base_lines)
            _timed_write(v1_path, full_bytes, timings["v1WriteNs"])
            started = time.perf_counter_ns()
            with message_path.open("ab") as handle:
                handle.write(suffix_lines)
            head_path.write_bytes(head_bytes)
            timings["v2WriteNs"].append(time.perf_counter_ns() - started)
            started = time.perf_counter_ns()
            recovered_v1 = json.loads(v1_path.read_text(encoding="utf-8"))
            timings["v1RecoveryNs"].append(time.perf_counter_ns() - started)
            started = time.perf_counter_ns()
            recovered_head = json.loads(head_path.read_text(encoding="utf-8"))
            recovered_head.pop("messageStore", None)
            recovered_head["messages"] = _recover_complete_jsonl(message_path)
            recovered_v2 = recovered_head
            timings["v2RecoveryNs"].append(time.perf_counter_ns() - started)
            if recovered_v1 != session or recovered_v2 != session:
                raise RuntimeError("v1/v2 prototype recovery diverged from the source session")
        return _prototype_report(
            root, session, messages, base_lines, suffix_lines, head_bytes, full_bytes,
            stable_watermark, timings, recovered_v2,
        )


def _timed_write(path: Path, content: bytes, target: list[int]) -> None:
    started = time.perf_counter_ns()
    path.write_bytes(content)
    target.append(time.perf_counter_ns() - started)


def _prototype_report(
    root: Path,
    session: dict[str, Any],
    messages: list[dict[str, Any]],
    base_lines: bytes,
    suffix_lines: bytes,
    head_bytes: bytes,
    full_bytes: bytes,
    stable_watermark: int,
    timings: dict[str, list[int]],
    recovered_v2: dict[str, Any] | None,
) -> dict[str, Any]:
    crash_path = root / "crash.messages.jsonl"
    crash_path.write_bytes(base_lines + suffix_lines + b'{"role":"assistant"')
    crash_recovered = _recover_complete_jsonl(crash_path)
    medians = {key: int(statistics.median(value)) for key, value in timings.items()}
    v1_bytes, v2_bytes = len(full_bytes), len(head_bytes) + len(suffix_lines)
    expected_digest, recovered_digest = _digest_json(session), _digest_json(recovered_v2)
    return {
        "v1": {
            "bytesPerCommit": v1_bytes,
            "physicalWriteCount": 1,
            "writeMedianNs": medians["v1WriteNs"],
            "recoveryMedianNs": medians["v1RecoveryNs"],
        },
        "v2Prototype": {
            "bytesPerCommit": v2_bytes,
            "physicalWriteCount": 2,
            "writeMedianNs": medians["v2WriteNs"],
            "recoveryMedianNs": medians["v2RecoveryNs"],
            "baseMessageCount": stable_watermark,
            "appendedMessageCount": len(messages) - stable_watermark,
        },
        "comparison": {
            "byteReductionRatio": round(1 - (v2_bytes / max(1, v1_bytes)), 6),
            "writeLatencyRatio": round(medians["v2WriteNs"] / max(1, medians["v1WriteNs"]), 6),
            "recoveryCostRatio": round(medians["v2RecoveryNs"] / max(1, medians["v1RecoveryNs"]), 6),
            "writeCountDelta": 1,
        },
        "crashBoundary": {
            "injected": "truncated trailing JSONL record after the committed watermark",
            "recoveredCompleteMessages": len(crash_recovered),
            "expectedCompleteMessages": len(messages),
            "passed": crash_recovered == messages,
        },
        "writeBack": {
            "expectedDigest": expected_digest,
            "recoveredDigest": recovered_digest,
            "exact": expected_digest == recovered_digest,
        },
        "samples": timings,
    }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _recover_complete_jsonl(path: Path) -> list[Any]:
    recovered = []
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            recovered.append(json.loads(raw))
        except json.JSONDecodeError:
            break
    return recovered


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


run_persistence_economics = runPersistenceEconomics
