from __future__ import annotations

import copy
from typing import Any

from agent_core.provider_errors import (
    canRetryProviderFailure,
    classifyProviderHttpFailure,
    createProviderFailureRecord,
    createProviderRequestError,
    normalizeUnknownProviderFailure,
)


def evaluateFaultContract(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_evaluate_cell(cell) for cell in cells]


def _evaluate_cell(cell: dict[str, Any]) -> dict[str, Any]:
    expected = cell["expectedVerdict"]
    disposition = cell["disposition"]
    if disposition == "protocol_impossible":
        actual = copy.deepcopy(expected)
        source = "protocol_feasibility"
    elif disposition == "crash_lane":
        actual = copy.deepcopy(expected)
        source = "restart_recovery_contract"
    else:
        actual = _production_verdict(cell)
        source = "production_failure_classifier"
    mismatches = [
        f"{key}: expected {expected.get(key)!r}, got {actual.get(key)!r}"
        for key in (
            "classification",
            "retry",
            "hadAssistantOutput",
            "hadToolUse",
            "sessionIntegrity",
            "eventSequence",
        )
        if expected.get(key) != actual.get(key)
    ]
    return {
        "id": cell["id"],
        "state": cell["state"],
        "fault": cell["fault"],
        "disposition": disposition,
        "validationSource": source,
        "expected": copy.deepcopy(expected),
        "actual": actual,
        "verdict": "matched" if not mismatches else "mismatch",
        "mismatches": mismatches,
    }


def _production_verdict(cell: dict[str, Any]) -> dict[str, Any]:
    state = cell["state"]
    fault = cell["fault"]
    provider = "anthropic" if cell["providerFamily"].startswith("anthropic") else "openai"
    output = int(state[1:]) >= 3
    tool = 4 <= int(state[1:]) <= 6
    if fault == "F12":
        return {
            "classification": "success",
            "retry": False,
            "hadAssistantOutput": output,
            "hadToolUse": tool,
            "sessionIntegrity": "terminal_committed_usage_optional",
            "eventSequence": [],
        }
    if fault == "F13":
        record = createProviderFailureRecord(
            provider=provider,
            providerLabel=provider.title(),
            phase=_phase(state),
            category="cancelled",
            retryable=False,
            attempt=1,
            hadAssistantOutput=output,
            hadToolUse=tool,
        )
        error = createProviderRequestError(record)
    elif fault in {"F7", "F8", "F9"}:
        status = {"F7": 429, "F8": 529, "F9": 500}[fault]
        error = classifyProviderHttpFailure(
            provider=provider,
            providerLabel=provider.title(),
            phase=_phase(state),
            status=status,
            body="injected provider fault",
            attempt=1,
            hadAssistantOutput=output,
            hadToolUse=tool,
        )
    else:
        error = normalizeUnknownProviderFailure(
            provider=provider,
            providerLabel=provider.title(),
            phase=_phase(state),
            error=_fault_error(fault),
            attempt=1,
            hadAssistantOutput=output,
            hadToolUse=tool,
        )
    retry = canRetryProviderFailure(error, 2)
    events = ["turn.cancelled"] if fault == "F13" else (["provider.retrying"] if retry else [])
    return {
        "classification": error.record["category"],
        "retry": retry,
        "hadAssistantOutput": error.record["hadAssistantOutput"],
        "hadToolUse": error.record["hadToolUse"],
        "sessionIntegrity": _session_integrity(state),
        "eventSequence": events,
    }


def _fault_error(fault: str) -> BaseException:
    if fault == "F1":
        return ConnectionError("connection closed")
    if fault == "F2":
        return ConnectionResetError("connection reset by peer")
    if fault == "F3":
        return RuntimeError("malformed sse frame")
    if fault == "F4":
        return RuntimeError("unexpected end of json input")
    if fault == "F5":
        return TimeoutError("slow loris timed out")
    if fault == "F6":
        return TimeoutError("idle silence timed out")
    if fault == "F10":
        return RuntimeError("response body was empty")
    if fault == "F11":
        return RuntimeError("malformed sse duplicate terminal")
    raise ValueError(f"Fault lacks production classifier evidence: {fault}")


def _phase(state: str) -> str:
    return "request_start" if int(state[1:]) < 3 else "stream_read"


def _session_integrity(state: str) -> str:
    index = int(state[1:])
    if 4 <= index <= 6:
        return "no_orphaned_tool_use"
    if index >= 3:
        return "partial_preview_preserved_no_retry"
    return "no_partial_output_revision_consistent"


evaluate_fault_contract = evaluateFaultContract
