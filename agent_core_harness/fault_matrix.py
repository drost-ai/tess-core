from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from .fake_providers import FakeProviderRuntime

REPORT_SCHEMA = "agent-core.provider-fault-matrix.v1"
STREAM_STATES = tuple(f"S{index}" for index in range(10))
FAULT_SHAPES = tuple(f"F{index}" for index in range(1, 15))
CONNECTION_MODES = ("fresh", "keep_alive")


async def runAnthropicFaultMatrixAsync() -> dict[str, Any]:
    cases = _anthropic_cases()
    _validate_cases(cases)
    results = [
        await _run_case(case, mode)
        for case in cases
        for mode in CONNECTION_MODES
    ]
    parity_mismatches = _connection_mode_mismatches(results)
    failed = [result for result in results if result["verdict"] != "matched"]
    executed_cells = {(result["state"], result["fault"]) for result in results}
    return {
        "schemaVersion": REPORT_SCHEMA,
        "providerFamily": "anthropic_messages_sse",
        "scope": "package45-dual-transport-cells",
        "status": "failed" if failed or parity_mismatches else "passed",
        "axes": {
            "streamStates": list(STREAM_STATES),
            "faultShapes": list(FAULT_SHAPES),
            "connectionModes": list(CONNECTION_MODES),
        },
        "coverage": {
            "declaredCells": len(STREAM_STATES) * len(FAULT_SHAPES),
            "executedCases": len(results),
            "executedUniqueCells": len(executed_cells),
            "remainingCells": len(STREAM_STATES) * len(FAULT_SHAPES) - len(executed_cells),
            "complete": False,
        },
        "connectionModeParity": {
            "status": "failed" if parity_mismatches else "passed",
            "mismatches": parity_mismatches,
        },
        "cases": results,
    }


def runAnthropicFaultMatrix() -> dict[str, Any]:
    return asyncio.run(runAnthropicFaultMatrixAsync())


def writeFaultMatrixReport(report: dict[str, Any], output: str) -> str:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


async def run_anthropic_fault_matrix_step(
    _data: dict[str, Any],
    _sandbox: Any,
    state: dict[str, Any],
) -> list[str]:
    report = await runAnthropicFaultMatrixAsync()
    state["provider_fault_matrix"] = report
    if report["status"] != "passed":
        mismatches = [case["id"] for case in report["cases"] if case["verdict"] != "matched"]
        raise RuntimeError(f"Anthropic fault-matrix mismatch: {', '.join(mismatches)}")
    return ["provider_fault_matrix"]


async def _run_case(case: dict[str, Any], mode: str) -> dict[str, Any]:
    runtime = FakeProviderRuntime()
    try:
        request = copy.deepcopy(case["request"])
        request["providerCapabilities"] = {
            "anthropicHttpKeepAlive": mode == "keep_alive"
        }
        result = await runtime.run(request)
        actual = _actual_verdict(result, runtime)
    finally:
        runtime.close()
    mismatches = _compare_verdict(case["expected"], actual)
    return {
        "id": f"{case['id']}[{mode}]",
        "caseId": case["id"],
        "connectionMode": mode,
        "title": case["title"],
        "state": case["state"],
        "fault": case["fault"],
        "tags": list(case.get("tags") or []),
        "expected": copy.deepcopy(case["expected"]),
        "actual": actual,
        "verdict": "matched" if not mismatches else "mismatch",
        "mismatches": mismatches,
    }


def _actual_verdict(result: dict[str, Any], runtime: FakeProviderRuntime) -> dict[str, Any]:
    event_names = [event["event"] for event in runtime.events]
    actual: dict[str, Any] = {
        "status": result["status"],
        "requestCount": len(runtime.requests),
        "eventNames": event_names,
        "connectionCount": len({request.get("connectionId") for request in runtime.requests}),
    }
    if result["status"] == "failed":
        failure = result.get("failure") or {}
        actual["failure"] = {
            key: failure.get(key)
            for key in ("category", "phase", "retryable", "hadAssistantOutput", "hadToolUse", "status")
        }
    else:
        response = result.get("response") or {}
        actual["response"] = {
            "content": response.get("content"),
            "usage": response.get("usage"),
            "stopReason": response.get("stopReason"),
        }
    return actual


def _compare_verdict(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for key in ("status", "requestCount", "failure", "response"):
        if key in expected and expected[key] != actual.get(key):
            mismatches.append(f"{key}: expected {expected[key]!r}, got {actual.get(key)!r}")
    event_names = actual["eventNames"]
    missing = [name for name in expected.get("eventsInclude", []) if name not in event_names]
    forbidden = [name for name in expected.get("eventsExclude", []) if name in event_names]
    if missing:
        mismatches.append(f"events missing: {missing}")
    if forbidden:
        mismatches.append(f"events unexpectedly present: {forbidden}")
    sequence = expected.get("eventSequence") or []
    positions = [event_names.index(name) for name in sequence if name in event_names]
    if len(positions) != len(sequence) or positions != sorted(positions):
        mismatches.append(f"event sequence not observed: {sequence}")
    return mismatches


def _validate_cases(cases: list[dict[str, Any]]) -> None:
    ids: set[str] = set()
    for case in cases:
        if case["id"] in ids:
            raise ValueError(f"Duplicate fault-matrix case id: {case['id']}")
        ids.add(case["id"])
        if case["state"] not in STREAM_STATES or case["fault"] not in FAULT_SHAPES:
            raise ValueError(f"Invalid matrix cell for {case['id']}: {case['state']}/{case['fault']}")
        expected = case.get("expected") or {}
        if expected.get("status") not in {"passed", "failed"} or "requestCount" not in expected:
            raise ValueError(f"Incomplete expected verdict for {case['id']}")


def _connection_mode_mismatches(results: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for result in results:
        actual = copy.deepcopy(result["actual"])
        actual.pop("connectionCount", None)
        grouped.setdefault(result["caseId"], {})[result["connectionMode"]] = actual
    mismatches = []
    for case_id, modes in grouped.items():
        if modes.get("fresh") != modes.get("keep_alive"):
            mismatches.append(case_id)
    return mismatches


def _anthropic_cases() -> list[dict[str, Any]]:
    success = _success_sse("recovered")
    common = {
        "provider": "anthropic",
        "api": "messages",
        "runtime": {"streamProviderMessages": True, "includePartialMessages": True},
    }
    return [
        {
            "id": "anthropic.s1.f7-rate-limit-retry",
            "title": "HTTP 429 before output retries and recovers",
            "state": "S1",
            "fault": "F7",
            "tags": ["retry", "output-free"],
            "request": {
                **common,
                "maxAttempts": 2,
                "script": {"messages": [_http_error(429, "rate_limit_error"), success]},
            },
            "expected": _success_after_retry(),
        },
        {
            "id": "anthropic.s1.f8-overloaded-retry",
            "title": "HTTP 529 before output retries and recovers",
            "state": "S1",
            "fault": "F8",
            "tags": ["retry", "output-free"],
            "request": {
                **common,
                "maxAttempts": 2,
                "script": {"messages": [_http_error(529, "overloaded_error"), success]},
            },
            "expected": _success_after_retry(),
        },
        {
            "id": "anthropic.s0.f1-disconnect-retry",
            "title": "Disconnect before headers retries as transport and recovers",
            "state": "S0",
            "fault": "F1",
            "tags": ["transport", "output-free", "package45-closed"],
            "request": {
                **common,
                "maxAttempts": 2,
                "script": {"messages": [{"fault": "disconnect_before_headers"}, success]},
            },
            "expected": _success_after_retry(),
        },
        {
            "id": "anthropic.s2.f6-idle-timeout",
            "title": "Headers followed by silence hit the activity deadline",
            "state": "S2",
            "fault": "F6",
            "tags": ["deadline", "output-free"],
            "request": {
                **common,
                "expectFailure": True,
                "maxAttempts": 1,
                "streamIdleTimeoutMs": 20,
                "script": {"messages": [{**success, "bodyDelayMs": 120}]},
            },
            "expected": _failure("timeout", "request_start", True, False),
        },
        {
            "id": "anthropic.s1.f4-malformed-frame",
            "title": "Malformed first SSE frame is a protocol failure",
            "state": "S1",
            "fault": "F4",
            "tags": ["protocol", "output-free"],
            "request": {
                **common,
                "expectFailure": True,
                "maxAttempts": 2,
                "script": {"messages": [_sse("event: message_start\ndata: {not-json}\n\n")]},
            },
            "expected": _failure("protocol", "parse", False, False),
        },
        {
            "id": "anthropic.s3.f1-partial-eof",
            "title": "EOF after text never retries partial output",
            "state": "S3",
            "fault": "F1",
            "tags": ["incident:package42", "output-aware"],
            "request": {
                **common,
                "expectFailure": True,
                "maxAttempts": 2,
                "script": {"messages": [_sse(_partial_sse("partial")), success]},
            },
            "expected": _failure("protocol", "stream_read", False, True),
        },
        {
            "id": "anthropic.s7.f12-usage-less-terminal",
            "title": "Usage-less terminal frame remains a valid response",
            "state": "S7",
            "fault": "F12",
            "tags": ["terminal", "usage"],
            "request": {**common, "maxAttempts": 1, "script": {"messages": [_success_sse("ok", usage=False)]}},
            "expected": {
                "status": "passed",
                "requestCount": 1,
                "response": {"content": [{"type": "text", "text": "ok"}], "usage": {}, "stopReason": "end_turn"},
                "eventsExclude": ["provider.retrying"],
            },
        },
        {
            "id": "anthropic.s2.f13-cancel",
            "title": "Abort during a silent response settles as cancelled",
            "state": "S2",
            "fault": "F13",
            "tags": ["cancel", "package45-baseline"],
            "request": {
                **common,
                "expectFailure": True,
                "abortAfterMs": 20,
                "maxAttempts": 2,
                "streamIdleTimeoutMs": 1_000,
                "script": {"messages": [{**success, "bodyDelayMs": 150}]},
            },
            "expected": _failure("cancelled", "request_start", False, False),
        },
    ]


def _success_after_retry() -> dict[str, Any]:
    return {
        "status": "passed",
        "requestCount": 2,
        "response": {
            "content": [{"type": "text", "text": "recovered"}],
            "usage": {"input_tokens": 9, "output_tokens": 3},
            "stopReason": "end_turn",
        },
        "eventsInclude": ["provider.retrying", "provider.recovered"],
        "eventSequence": ["provider.retrying", "provider.recovered"],
    }


def _failure(category: str, phase: str, retryable: bool, output: bool) -> dict[str, Any]:
    return {
        "status": "failed",
        "requestCount": 1,
        "failure": {
            "category": category,
            "phase": phase,
            "retryable": retryable,
            "hadAssistantOutput": output,
            "hadToolUse": False,
            "status": None,
        },
        "eventsExclude": ["provider.recovered"],
    }


def _http_error(status: int, error_type: str) -> dict[str, Any]:
    return {"status": status, "body": {"error": {"type": error_type, "message": "injected fault"}}}


def _sse(body: str) -> dict[str, Any]:
    return {"contentType": "text/event-stream", "rawBody": body}


def _partial_sse(text: str) -> str:
    return "".join(
        (
            'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":9}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"{text}"}}}}\n\n',
        )
    )


def _success_sse(text: str, *, usage: bool = True) -> dict[str, Any]:
    start_usage = ',"usage":{"input_tokens":9}' if usage else ""
    delta_usage = ',"usage":{"output_tokens":3}' if usage else ""
    body = "".join(
        (
            f'event: message_start\ndata: {{"type":"message_start","message":{{"model":"claude-sonnet-4-6"{start_usage}}}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"{text}"}}}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            f'event: message_delta\ndata: {{"type":"message_delta","delta":{{"stop_reason":"end_turn"}}{delta_usage}}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        )
    )
    return _sse(body)
