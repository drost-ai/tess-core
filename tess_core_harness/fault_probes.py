from __future__ import annotations

import copy
from typing import Any

from .fake_providers import FakeProviderRuntime


async def runOpenAiFaultProbesAsync() -> list[dict[str, Any]]:
    cases = _openai_cases()
    return [await _run_case(case) for case in cases]


async def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    runtime = FakeProviderRuntime()
    try:
        result = await runtime.run(copy.deepcopy(case["request"]))
        actual = _actual_verdict(result, runtime)
    finally:
        runtime.close()
    mismatches = _compare(case["expected"], actual)
    return {
        "id": case["id"],
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
    actual: dict[str, Any] = {
        "status": result["status"],
        "requestCount": len(runtime.requests),
        "eventNames": [event["event"] for event in runtime.events],
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


def _compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    mismatches = []
    for key in ("status", "requestCount", "failure", "response"):
        if key in expected and expected[key] != actual.get(key):
            mismatches.append(f"{key}: expected {expected[key]!r}, got {actual.get(key)!r}")
    names = actual["eventNames"]
    for name in expected.get("eventsInclude", []):
        if name not in names:
            mismatches.append(f"event missing: {name}")
    for name in expected.get("eventsExclude", []):
        if name in names:
            mismatches.append(f"event unexpectedly present: {name}")
    sequence = expected.get("eventSequence") or []
    cursor = -1
    for name in sequence:
        try:
            cursor = names.index(name, cursor + 1)
        except ValueError:
            mismatches.append(f"event sequence not observed: {sequence}")
            break
    return mismatches


def _openai_cases() -> list[dict[str, Any]]:
    common = {
        "provider": "openai",
        "api": "responses",
        "runtime": {"streamProviderMessages": True, "includePartialMessages": True},
    }
    completed = _completed("recovered", usage=True)
    return [
        {
            "id": "openai.s0.f1-create-retry",
            "title": "Output-free SDK create failure retries and recovers",
            "state": "S0",
            "fault": "F1",
            "tags": ["incident:package43", "output-free", "retry"],
            "request": {
                **common,
                "maxAttempts": 2,
                "script": {"attempts": [{"createError": "socket hang up"}, completed]},
            },
            "expected": {
                "status": "passed",
                "requestCount": 2,
                "response": {
                    "content": [{"type": "text", "text": "recovered"}],
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                    "stopReason": "stop",
                },
                "eventsInclude": ["provider.retrying", "provider.recovered"],
                "eventSequence": ["provider.retrying", "provider.recovered"],
            },
        },
        {
            "id": "openai.s3.f1-partial-stream-error",
            "title": "Partial-output stream failure is classified without retry",
            "state": "S3",
            "fault": "F1",
            "tags": ["incident:package42", "incident:package43", "output-aware"],
            "request": {
                **common,
                "expectFailure": True,
                "maxAttempts": 2,
                "script": {"attempts": [{"stream": [
                    {"event": {"type": "response.output_text.delta", "delta": "partial"}},
                    {"event": {"type": "response.failed", "response": {"error": {"code": "server_error", "message": "stream failed"}}}},
                ]}, completed]},
            },
            "expected": {
                "status": "failed",
                "requestCount": 1,
                "failure": {
                    "category": "server",
                    "phase": "stream_read",
                    "retryable": True,
                    "hadAssistantOutput": True,
                    "hadToolUse": False,
                    "status": None,
                },
                "eventsExclude": ["provider.retrying", "provider.recovered"],
            },
        },
        {
            "id": "openai.s7.f12-usage-less-terminal",
            "title": "Usage-less completed event remains a valid response",
            "state": "S7",
            "fault": "F12",
            "tags": ["terminal", "usage"],
            "request": {**common, "script": {"attempts": [_completed("ok", usage=False)]}},
            "expected": {
                "status": "passed",
                "requestCount": 1,
                "response": {
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {},
                    "stopReason": "stop",
                },
                "eventsExclude": ["provider.retrying"],
            },
        },
        {
            "id": "openai.s1.f13-cancel-create",
            "title": "Abort while SDK create is pending settles as cancelled",
            "state": "S1",
            "fault": "F13",
            "tags": ["cancel", "package45-baseline"],
            "request": {
                **common,
                "expectFailure": True,
                "abortAfterMs": 10,
                "maxAttempts": 2,
                "requestStartTimeoutMs": 1_000,
                "script": {"attempts": [{"createDelayMs": 100, "stream": completed["stream"]}]},
            },
            "expected": {
                "status": "failed",
                "requestCount": 1,
                "failure": {
                    "category": "cancelled",
                    "phase": "request_start",
                    "retryable": False,
                    "hadAssistantOutput": False,
                    "hadToolUse": False,
                    "status": None,
                },
                "eventsExclude": ["provider.retrying", "provider.recovered"],
            },
        },
    ]


def _completed(text: str, *, usage: bool) -> dict[str, Any]:
    response: dict[str, Any] = {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }
    if usage:
        response["usage"] = {"input_tokens": 4, "output_tokens": 2}
    return {"stream": [{"event": {"type": "response.completed", "response": response}}]}


run_openai_fault_probes_async = runOpenAiFaultProbesAsync
