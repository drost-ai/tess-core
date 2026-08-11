from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import agent_core._facade as core
from agent_core.providers.errors import (
    can_retry_provider_failure,
    create_provider_request_error,
    normalize_unknown_provider_failure,
)
from agent_core.providers.anthropic.sse import (
    AnthropicSseAccumulator,
    parse_anthropic_sse_events,
)
from agent_core.providers.openai.transport import _raise_openai_cancelled

from .fake_providers import FakeProviderRuntime
from .fake_providers.server import openaiResponsesSseBody
from .fault_crash_execution import executeCrashFaultCell
from .fault_session_evidence import verifyFaultSessionIntegrity
async def executeFaultContractAsync(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="agent-core-fault-contract-") as tmp:
        root = Path(tmp)
        home = root / "home"
        home.mkdir()
        async with core.scoped_runtime_home(str(home)):
            return [await _execute_cell(cell, root) for cell in cells]


async def _execute_cell(cell: dict[str, Any], root: Path) -> dict[str, Any]:
    expected = cell["expectedVerdict"]
    if cell["disposition"] == "protocol_impossible":
        return {
            "id": cell["id"],
            "state": cell["state"],
            "fault": cell["fault"],
            "disposition": cell["disposition"],
            "validationSource": "protocol_state_machine_feasibility",
            "executed": False,
            "classifiedImpossible": True,
            "expected": copy.deepcopy(expected),
            "actual": copy.deepcopy(expected),
            "verdict": "matched",
            "mismatches": [],
            "evidence": {"rationale": cell["rationale"]},
        }
    if cell["disposition"] == "crash_lane":
        actual, evidence = await executeCrashFaultCell(cell, root)
        source = "sigkill_restart_recovery"
    else:
        actual, evidence = await _execute_adapter_cell(cell)
        source = evidence.pop("adapterInvocation")
    if cell["disposition"] != "crash_lane":
        integrity, session_evidence, event_sequence = await verifyFaultSessionIntegrity(
            cell, actual, root
        )
        actual["sessionIntegrity"] = integrity
        actual["eventSequence"] = event_sequence
        evidence["session"] = session_evidence
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
        "disposition": cell["disposition"],
        "validationSource": source,
        "executed": True,
        "classifiedImpossible": False,
        "expected": copy.deepcopy(expected),
        "actual": actual,
        "verdict": "matched" if not mismatches else "mismatch",
        "mismatches": mismatches,
        "evidence": evidence,
    }

async def _execute_adapter_cell(cell: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    provider = cell["providerFamily"]
    if provider == "anthropic_messages_sse":
        return await _execute_anthropic_adapter(cell)
    return await _execute_openai_adapter(cell)


async def _execute_anthropic_adapter(cell: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    state, fault = cell["state"], cell["fault"]
    if fault in {"F1", "F2", "F5", "F6", "F7", "F8", "F9"}:
        return await _execute_anthropic_wire_cell(cell)
    events: list[tuple[str, dict[str, Any]]] = []
    accumulator = AnthropicSseAccumulator(
        {"model": "claude-sonnet-4-6"},
        {
            "streamProviderMessages": True,
            "includePartialMessages": True,
            "emitEvent": lambda event, data: events.append((event, data)),
        },
        1,
    )
    for event in parse_anthropic_sse_events(_anthropic_state_prefix(state)):
        accumulator.process_raw_event(event)
    error = None
    response = None
    invocation = "AnthropicSseAccumulator.process_raw_event"
    try:
        if fault in {"F3", "F4"}:
            accumulator.process_raw_event(
                {"event": "content_block_delta", "data": '{"type":'}
            )
        elif fault == "F10":
            response = accumulator.finalize()
        elif fault == "F11":
            accumulator.process_raw_event(
                {"event": "message_stop", "data": '{"type":"message_stop"}'}
            )
            response = accumulator.finalize()
        elif fault == "F12":
            accumulator.process_raw_event(
                {
                    "event": "message_delta",
                    "data": '{"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
                }
            )
            accumulator.process_raw_event(
                {"event": "message_stop", "data": '{"type":"message_stop"}'}
            )
            response = accumulator.finalize()
        elif fault == "F13":
            from agent_core.providers.anthropic.transport import _raise_anthropic_cancelled

            _raise_anthropic_cancelled(
                {"abortSignal": {"aborted": True}},
                _phase(state),
                1,
                accumulator.had_assistant_output,
                accumulator.had_tool_use,
            )
    except Exception as exc:
        error = normalize_unknown_provider_failure(
            error=exc,
            provider="anthropic",
            providerLabel="Anthropic",
            phase=_phase(state),
            attempt=1,
            hadAssistantOutput=accumulator.had_assistant_output,
            hadToolUse=accumulator.had_tool_use,
        )
    actual = _actual_from_result(
        error,
        response,
        output=accumulator.had_assistant_output,
        tool=accumulator.had_tool_use,
    )
    return actual, {
        "adapterInvocation": invocation,
        "eventNames": [event for event, _data in events],
        "statePrefixDigest": _digest(_anthropic_state_prefix(state)),
    }


async def _execute_openai_adapter(cell: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    state, fault = cell["state"], cell["fault"]
    output = int(state[1:]) >= 3
    tool = 4 <= int(state[1:]) <= 6
    error = None
    response = None
    event_names: list[str] = []
    invocation = "send_openai_responses_request(FakeOpenAIClientFactory)"
    if fault in {"F3", "F5", "F6", "F7", "F8", "F9"}:
        data: dict[str, Any] = {
            "provider": "openai",
            "api": "responses",
            "wire": True,
            "expectFailure": True,
            "maxAttempts": 1,
            "runtime": {"streamProviderMessages": True, "includePartialMessages": True},
        }
        if fault in {"F7", "F8", "F9"}:
            status = {"F7": 429, "F8": 529, "F9": 500}[fault]
            data["script"] = {
                "responses": [
                    {
                        "status": status,
                        "contentType": "application/json",
                        "body": {"error": {"type": "injected_status", "message": "injected provider status"}},
                    }
                ]
            }
        else:
            prefix_events = [
                action["event"]
                for action in _openai_state_prefix(state)
                if "event" in action
            ]
            body = openaiResponsesSseBody(prefix_events)
            if fault == "F3":
                # Malformed SSE bytes on the wire.
                data["script"] = {
                    "responses": [
                        {
                            "contentType": "text/event-stream",
                            "rawBody": body + "event: response.output_text.delta\ndata: {broken\n\n",
                        }
                    ]
                }
            else:
                if fault == "F5":
                    # Slow loris: a partial frame trickles, never completing.
                    chunks = ([{"raw": body}] if body else []) + [
                        {"raw": "event: response.out"},
                        {"delayMs": 5_000, "raw": "put_text.delta\n"},
                    ]
                else:
                    # Idle silence after the prefix.
                    chunks = ([{"raw": body}] if body else []) + [
                        {"delayMs": 5_000, "raw": ": stalled\n\n"}
                    ]
                data["script"] = {"responses": [{"contentType": "text/event-stream", "chunks": chunks}]}
                data["streamIdleTimeoutMs"] = 80
        return await _run_wire_fault_cell(
            cell, data, "send_openai_responses_request(FakeProviderHttpServer wire)"
        )
    elif fault == "F13":
        try:
            _raise_openai_cancelled(
                {"abortSignal": {"aborted": True}},
                "OpenAI",
                _phase(state),
                1,
                output,
                tool,
            )
        except Exception as exc:
            error = exc
        invocation = "_raise_openai_cancelled"
    else:
        runtime = FakeProviderRuntime()
        try:
            actions = _openai_state_prefix(state)
            if fault in {"F1", "F2"}:
                if state in {"S0", "S1"}:
                    attempt = {"createError": "socket hang up"}
                else:
                    attempt = {"stream": [*actions, {"raise": "socket hang up"}]}
            elif fault == "F4":
                attempt = {
                    "stream": [
                        *actions,
                        {"event": {"type": "response.output_item.added", "item": {}}},
                    ]
                }
            elif fault == "F10":
                attempt = {"stream": actions}
            elif fault == "F11":
                attempt = {"stream": [*actions, {"event": _openai_completed(usage=True)}]}
            elif fault == "F12":
                attempt = {"stream": [*actions, {"event": _openai_completed(usage=False)}]}
            else:
                raise RuntimeError(f"Unsupported OpenAI adapter fault: {fault}")
            result = await runtime.run(
                {
                    "provider": "openai",
                    "api": "responses",
                    "expectFailure": True,
                    "maxAttempts": 1,
                    "runtime": {
                        "streamProviderMessages": True,
                        "includePartialMessages": True,
                    },
                    "script": {"attempts": [attempt]},
                }
            )
            event_names = [item["event"] for item in runtime.events]
            if result["status"] == "failed":
                error = create_provider_request_error(result["failure"])
            else:
                response = result.get("response") or {}
        finally:
            runtime.close()
    actual = _actual_from_result(error, response, output=output, tool=tool)
    return actual, {
        "adapterInvocation": invocation,
        "eventNames": event_names,
        "statePrefixDigest": _digest(json.dumps(_openai_state_prefix(state), sort_keys=True)),
    }


def _actual_from_result(
    error: Any,
    response: Any,
    *,
    output: bool,
    tool: bool,
) -> dict[str, Any]:
    if error is None and response is not None:
        return {
            "classification": "success",
            "retry": False,
            "hadAssistantOutput": output,
            "hadToolUse": tool,
            "eventSequence": [],
        }
    if error is None:
        raise RuntimeError("Fault adapter produced neither an error nor a response")
    record = error.record
    return {
        "classification": record.get("category"),
        "retryable": bool(record.get("retryable")),
        "retry": can_retry_provider_failure(error, 2),
        "hadAssistantOutput": bool(record.get("hadAssistantOutput")),
        "hadToolUse": bool(record.get("hadToolUse")),
        "eventSequence": [],
    }


async def _execute_anthropic_wire_cell(cell: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inject the fault through a real socket and the production request path."""
    state, fault = cell["state"], cell["fault"]
    prefix = _anthropic_state_prefix(state)
    data: dict[str, Any] = {
        "provider": "anthropic",
        "api": "messages",
        "expectFailure": True,
        "maxAttempts": 1,
        "runtime": {"streamProviderMessages": True, "includePartialMessages": True},
    }
    if fault in {"F7", "F8", "F9"}:
        status = {"F7": 429, "F8": 529, "F9": 500}[fault]
        data["script"] = {
            "messages": [
                {
                    "status": status,
                    "contentType": "application/json",
                    "body": {"error": {"type": "injected_status", "message": "injected provider status"}},
                }
            ]
        }
    elif fault in {"F1", "F2"}:
        if state in {"S0", "S1"}:
            data["script"] = {"messages": [{"fault": "disconnect_before_headers"}]}
        else:
            data["script"] = {
                "messages": [
                    {
                        "contentType": "text/event-stream",
                        "rawBody": prefix + ": padding\n\n",
                        "truncateAfterBytes": max(1, len(prefix.encode("utf-8"))),
                    }
                ]
            }
    else:
        chunks = ([{"raw": prefix}] if prefix else []) + [
            {"delayMs": 5_000, "raw": ": stalled\n\n"}
        ]
        data["script"] = {"messages": [{"contentType": "text/event-stream", "chunks": chunks}]}
        data["streamIdleTimeoutMs"] = 80
    return await _run_wire_fault_cell(cell, data, "send_anthropic_messages_request(FakeProviderHttpServer)")


async def _run_wire_fault_cell(
    cell: dict[str, Any], data: dict[str, Any], invocation: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = cell["state"]
    output = int(state[1:]) >= 3
    tool = 4 <= int(state[1:]) <= 6
    runtime = FakeProviderRuntime()
    try:
        result = await runtime.run(data)
    finally:
        runtime.close()
    error = (
        create_provider_request_error(result["failure"])
        if result["status"] == "failed"
        else None
    )
    response = result.get("response") if error is None else None
    actual = _actual_from_result(error, response, output=output, tool=tool)
    return actual, {
        "adapterInvocation": invocation,
        "eventNames": [item["event"] for item in runtime.events],
        "statePrefixDigest": _digest(json.dumps(data.get("script"), sort_keys=True, default=str)),
    }


def _anthropic_state_prefix(state: str) -> str:
    index = int(state[1:])
    if index < 2:
        return ""
    frames = [
        _anthropic_frame(
            "message_start",
            {"type": "message_start", "message": {"model": "claude-sonnet-4-6", "usage": {"input_tokens": 1}}},
        )
    ]
    if index == 2:
        return "".join(frames)
    frames.extend(
        [
            _anthropic_frame("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            _anthropic_frame("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}}),
        ]
    )
    if index == 3:
        return "".join(frames)
    frames.append(_anthropic_frame("content_block_stop", {"type": "content_block_stop", "index": 0}))
    if 4 <= index <= 6:
        frames.append(_anthropic_frame("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}}}))
        if index >= 5:
            frames.append(_anthropic_frame("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}}))
        if index >= 6:
            frames.append(_anthropic_frame("content_block_stop", {"type": "content_block_stop", "index": 1}))
        return "".join(frames)
    if index >= 8:
        frames.append(_anthropic_frame("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}))
    if index >= 9:
        frames.append(_anthropic_frame("message_stop", {"type": "message_stop"}))
    return "".join(frames)


def _anthropic_frame(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _openai_state_prefix(state: str) -> list[dict[str, Any]]:
    index = int(state[1:])
    actions: list[dict[str, Any]] = []
    if index < 2:
        return actions
    actions.append({"event": {"type": "response.created", "response": {"id": "response-1"}}})
    if index == 2:
        return actions
    actions.append({"event": {"type": "response.output_text.delta", "delta": "partial"}})
    if index == 3:
        return actions
    if 4 <= index <= 6:
        actions.append({"event": {"type": "response.output_item.added", "item": {"type": "function_call", "id": "item-1", "call_id": "call-1", "name": "Read"}}})
        if index >= 5:
            actions.append({"event": {"type": "response.function_call_arguments.done", "item_id": "item-1", "name": "Read", "arguments": "{}"}})
        if index >= 6:
            actions.append({"event": {"type": "response.output_item.done", "item": {"type": "function_call", "id": "item-1", "call_id": "call-1", "name": "Read", "arguments": "{}"}}})
        return actions
    if index >= 9:
        actions.append({"event": _openai_completed(usage=True)})
    return actions


def _openai_completed(*, usage: bool) -> dict[str, Any]:
    response: dict[str, Any] = {
        "id": "response-1",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}],
    }
    if usage:
        response["usage"] = {"input_tokens": 1, "output_tokens": 1}
    return {"type": "response.completed", "response": response}


def _phase(state: str) -> str:
    return "request_start" if state in {"S0", "S1", "S2"} else "stream_read"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


execute_fault_contract_async = executeFaultContractAsync
