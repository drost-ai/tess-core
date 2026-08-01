from __future__ import annotations

from typing import Any

STREAM_STATES = tuple(f"S{index}" for index in range(10))
FAULT_SHAPES = tuple(f"F{index}" for index in range(1, 15))
PROVIDER_FAMILIES = ("anthropic_messages_sse", "openai_responses")

STATE_LABELS = {
    "S0": "pre_connect",
    "S1": "connected_no_bytes",
    "S2": "headers_only",
    "S3": "mid_text_delta",
    "S4": "mid_tool_use_open",
    "S5": "mid_tool_use_arguments",
    "S6": "tool_use_closed",
    "S7": "pre_terminal",
    "S8": "terminal_frame_partial",
    "S9": "post_terminal_pre_close",
}
FAULT_LABELS = {
    "F1": "tcp_disconnect",
    "F2": "tls_or_reset_mid_frame",
    "F3": "malformed_sse_frame",
    "F4": "truncated_json_frame",
    "F5": "slow_loris",
    "F6": "idle_silence",
    "F7": "http_429",
    "F8": "http_529",
    "F9": "http_5xx_json",
    "F10": "empty_response",
    "F11": "duplicate_terminal",
    "F12": "usage_less_terminal",
    "F13": "cancel",
    "F14": "process_kill",
}


def buildFaultContract(provider_family: str) -> list[dict[str, Any]]:
    if provider_family not in PROVIDER_FAMILIES:
        raise ValueError(f"Unsupported provider family: {provider_family}")
    cells = [
        _cell(provider_family, state, fault)
        for state in STREAM_STATES
        for fault in FAULT_SHAPES
    ]
    validateFaultContract(cells)
    return cells


def validateFaultContract(cells: list[dict[str, Any]]) -> None:
    expected = {(state, fault) for state in STREAM_STATES for fault in FAULT_SHAPES}
    observed = {(cell.get("state"), cell.get("fault")) for cell in cells}
    if observed != expected or len(cells) != len(expected):
        missing = sorted(expected - observed)
        duplicates = len(cells) - len(observed)
        raise ValueError(f"Fault contract is incomplete: missing={missing}, duplicates={duplicates}")
    for cell in cells:
        if cell.get("disposition") not in {"adapter_probe", "crash_lane", "protocol_impossible"}:
            raise ValueError(f"Unknown disposition for {cell.get('id')}")
        verdict = cell.get("expectedVerdict")
        if not isinstance(verdict, dict) or not verdict.get("classification"):
            raise ValueError(f"Missing verdict for {cell.get('id')}")
        if cell.get("disposition") == "protocol_impossible" and not cell.get("rationale"):
            raise ValueError(f"Protocol-impossible cell lacks rationale: {cell.get('id')}")


def summarizeFaultContract(cells: list[dict[str, Any]]) -> dict[str, Any]:
    dispositions = {name: 0 for name in ("adapter_probe", "crash_lane", "protocol_impossible")}
    for cell in cells:
        dispositions[cell["disposition"]] += 1
    return {
        "declaredCells": len(STREAM_STATES) * len(FAULT_SHAPES),
        "classifiedCells": len(cells),
        "unknownCells": 0,
        "remainingCells": 0,
        "complete": True,
        "dispositions": dispositions,
    }


def _cell(provider: str, state: str, fault: str) -> dict[str, Any]:
    impossible = _impossible_reason(provider, state, fault)
    if impossible:
        disposition = "protocol_impossible"
        verdict = {
            "classification": "not_applicable",
            "retry": False,
            "hadAssistantOutput": _has_output(state),
            "hadToolUse": _has_tool(state),
            "sessionIntegrity": "unchanged_no_injection",
            "eventSequence": [],
        }
    else:
        disposition = "crash_lane" if fault == "F14" else "adapter_probe"
        verdict = _expected_verdict(state, fault)
    return {
        "id": f"{provider}.{state.lower()}.{fault.lower()}",
        "providerFamily": provider,
        "state": state,
        "stateLabel": STATE_LABELS[state],
        "fault": fault,
        "faultLabel": FAULT_LABELS[fault],
        "disposition": disposition,
        "rationale": impossible or _disposition_rationale(disposition),
        "expectedVerdict": verdict,
    }


def _impossible_reason(provider: str, state: str, fault: str) -> str | None:
    state_index = int(state[1:])
    if fault in {"F7", "F8", "F9"} and state_index >= 2:
        return "An HTTP status is settled before response headers and cannot first occur in this stream state."
    if fault in {"F10", "F11", "F12"} and state_index < 7:
        return "This terminal-response shape cannot be known before the pre-terminal state."
    if provider == "openai_responses" and fault == "F3":
        return "The SDK adapter yields decoded events; malformed SSE bytes are below this adapter boundary."
    if provider == "openai_responses" and fault == "F5":
        return "Byte pacing is below the fake SDK adapter boundary; its observable equivalent is F6 idle silence."
    return None


def _expected_verdict(state: str, fault: str) -> dict[str, Any]:
    output = _has_output(state)
    tool = _has_tool(state)
    if fault == "F12":
        return {
            "classification": "success",
            "retry": False,
            "hadAssistantOutput": output,
            "hadToolUse": tool,
            "sessionIntegrity": "terminal_committed_usage_optional",
            "eventSequence": [],
        }
    categories = {
        "F1": "transport",
        "F2": "transport",
        "F3": "protocol",
        "F4": "protocol",
        "F5": "timeout",
        "F6": "timeout",
        "F7": "rate_limit",
        "F8": "overloaded",
        "F9": "provider_http",
        "F10": "protocol",
        "F11": "protocol",
        "F13": "cancelled",
        "F14": "process_terminated",
    }
    retryable = fault in {"F1", "F2", "F5", "F6", "F7", "F8", "F9"}
    retry = retryable and not output and not tool
    events = ["provider.retrying"] if retry else []
    if fault == "F13":
        events = ["turn.cancelled"]
    return {
        "classification": categories[fault],
        "retry": retry,
        "hadAssistantOutput": output,
        "hadToolUse": tool,
        "sessionIntegrity": _session_integrity(state, fault),
        "eventSequence": events,
    }


def _has_output(state: str) -> bool:
    return int(state[1:]) >= 3


def _has_tool(state: str) -> bool:
    return 4 <= int(state[1:]) <= 6


def _session_integrity(state: str, fault: str) -> str:
    if fault == "F14":
        return "restart_repair_no_duplicate_reconciliation"
    if _has_tool(state):
        return "no_orphaned_tool_use"
    if _has_output(state):
        return "partial_preview_preserved_no_retry"
    return "no_partial_output_revision_consistent"


def _disposition_rationale(disposition: str) -> str:
    if disposition == "crash_lane":
        return "Validated by restart-recovery tests because an in-process adapter cannot survive its own kill."
    return "Executable at the provider-adapter boundary; representative probes are regression-gated."


build_fault_contract = buildFaultContract
validate_fault_contract = validateFaultContract
