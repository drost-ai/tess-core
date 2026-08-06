from __future__ import annotations

import json
import re
from typing import Any

from ..structured_value import summarizeStructuredValue
from ..tool_payloads import getToolPayloadPolicy
from ..utf8_bounds import normalizeUtf8String, sliceUtf8Prefix, utf8ByteLength


def shapeForegroundSubagentResult(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    policy = getToolPayloadPolicy(values["config"])
    max_serialized_bytes = policy["textInlineBytes"]
    structured = summarizeStructuredValue(
        values.get("structuredResult"),
        {
            "maxDepth": 5,
            "maxKeys": 30,
            "maxItems": 50,
            "maxStringBytes": 1_000,
            "maxBytes": max(800, int(max_serialized_bytes * 0.2)),
        },
    )
    structured_errors = summarizeStructuredValue(
        values.get("structuredResultErrors"),
        {"maxDepth": 3, "maxKeys": 20, "maxItems": 20, "maxStringBytes": 500, "maxBytes": 1_000},
    )
    authority = summarizeStructuredValue(
        values.get("effectiveAuthority"),
        {"maxDepth": 4, "maxKeys": 30, "maxItems": 40, "maxStringBytes": 500, "maxBytes": 1_500},
    )
    runtime_contract = _summarize_runtime_contract(values.get("runtimeContract"))
    text = normalizeUtf8String(str(values.get("text") or ""))
    text_bytes = utf8ByteLength(text)
    agent_id = sliceUtf8Prefix(str(values.get("agentId") or ""), 256)
    session_id = sliceUtf8Prefix(str(values.get("sessionId") or ""), 256)
    base = _without_none(
        {
            "type": "foreground_subagent_result",
            "agent_id": agent_id,
            "session_id": session_id,
            "agent_type": (
                sliceUtf8Prefix(str(values["agentType"]), 256)
                if values.get("agentType")
                else None
            ),
            "status": sliceUtf8Prefix(str(values.get("status") or ""), 80),
            "usage": summarizeStructuredValue(
                values.get("usage"), {"maxDepth": 3, "maxItems": 20, "maxBytes": 800}
            )["value"],
            "tool_count": values.get("toolCount"),
            "duration_ms": values.get("durationMs"),
            "stop_reason": (
                sliceUtf8Prefix(str(values["stopReason"]), 200)
                if values.get("stopReason")
                else None
            ),
            "worktree_path": (
                sliceUtf8Prefix(str(values["worktreePath"]), 1_000)
                if values.get("worktreePath")
                else None
            ),
            "structured_result_status": values.get("structuredResultStatus"),
            "structured_result": structured["value"],
            "structured_result_truncated": True if structured["truncated"] else None,
            "structured_result_errors": structured_errors["value"],
            "effective_authority": authority["value"],
            "runtime_contract": runtime_contract["value"],
            "runtime_contract_truncated": True if runtime_contract["truncated"] else None,
            "recovery": {
                "type": "child_session",
                "session_id": session_id,
                "agent_id": agent_id,
                "note": "The child session transcript is the durable source for omitted result content.",
            },
            "result_bytes": text_bytes,
        }
    )
    excerpt = _fit_text_field(base, text, max_serialized_bytes)
    return {
        **base,
        "content": excerpt,
        **({"content_truncated": True} if utf8ByteLength(excerpt) < text_bytes else {}),
    }


def boundedSubagentEventSummary(value: str) -> str:
    return sliceUtf8Prefix(sliceUtf8Prefix(value, 2_000).strip(), 1_000)


def boundedSubagentError(error: Any) -> str:
    try:
        message = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    except Exception:
        message = "Unknown subagent failure"
    bounded = sliceUtf8Prefix(message or "Unknown subagent failure", 4_000)
    patterns = (
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
        re.compile(
            r"\b(access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret)\s*[:=]\s*[^\s,;]+",
            re.I,
        ),
    )
    for pattern in patterns:
        bounded = pattern.sub("[REDACTED]", bounded)
    return sliceUtf8Prefix(bounded, 2_000)


def boundedSubagentRuntimeContract(value: Any) -> Any:
    return _summarize_runtime_contract(value)["value"]


def _summarize_runtime_contract(value: Any) -> dict[str, Any]:
    return summarizeStructuredValue(
        value,
        {
            "maxDepth": 5,
            "maxKeys": 30,
            "maxItems": 50,
            "maxStringBytes": 500,
            "maxBytes": 1_500,
        },
    )


def _fit_text_field(base: dict[str, Any], source: str, max_serialized_bytes: int) -> str:
    source_bytes = utf8ByteLength(source)
    low = 0
    high = min(source_bytes, max_serialized_bytes)
    best = ""
    while low <= high:
        midpoint = (low + high) // 2
        excerpt = sliceUtf8Prefix(source, midpoint)
        candidate = {
            **base,
            "content": excerpt,
            **({"content_truncated": True} if utf8ByteLength(excerpt) < source_bytes else {}),
        }
        if _serialized_content_bytes(candidate) <= max_serialized_bytes:
            best = excerpt
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _serialized_content_bytes(value: dict[str, Any]) -> int:
    content = json.dumps(value, indent=2, ensure_ascii=False)
    return utf8ByteLength(json.dumps(content, ensure_ascii=False, separators=(",", ":")))


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


shape_foreground_subagent_result = shapeForegroundSubagentResult
bounded_subagent_event_summary = boundedSubagentEventSummary
bounded_subagent_error = boundedSubagentError
bounded_subagent_runtime_contract = boundedSubagentRuntimeContract
