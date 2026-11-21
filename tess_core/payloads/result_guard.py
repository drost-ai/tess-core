from __future__ import annotations

import inspect
import json
import re
from typing import Any, Callable

from .tools import _persist_artifact as _persist_tool_output_artifact
from .tools import build_persisted_output, inline_output_chars
from .utf8_bounds import (
    assert_utf8_byte_limit,
    build_utf8_head_tail_sample,
    normalize_utf8_string,
    slice_utf8_prefix,
    utf8_byte_length,
)


MAX_TOOL_IDENTITY_BYTES = 256
MAX_ERROR_MESSAGE_BYTES = 2_000
ERROR_ENVELOPE_BYTES = 4_000
MIN_EFFECTIVE_TOOL_RESULT_BYTES = 512
ABSOLUTE_TOOL_RESULT_CEILING_BYTES = 512_000


def _limit_chars(values: dict[str, Any]) -> int | None:
    """The most characters this result may carry inline, or None for a tool that bounds its own results.

    Every tool gets the same allowance unless it declares its own (ToolDefinition.maxResultChars):
    a tool that pages its own output, as Read does, declares 0 and is held only to the absolute ceiling.
    """
    default = inline_output_chars()
    declared = values.get("maxChars")
    if declared is not None:
        try:
            parsed = int(declared)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("maxChars must be a non-negative integer") from None
        if parsed < 0:
            raise ValueError("maxChars must be a non-negative integer")
        return parsed or None
    requested = values.get("maxBytes")
    if requested is None:
        return default
    try:
        parsed = int(requested)
        if parsed <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("maxBytes must be a positive integer") from None
    if values.get("allowRaise"):
        # Inter-agent payloads may exceed the default; the absolute ceiling still bounds a runaway result.
        return max(default, min(ABSOLUTE_TOOL_RESULT_CEILING_BYTES, parsed))
    return min(default, max(MIN_EFFECTIVE_TOOL_RESULT_BYTES, parsed))


async def guard_tool_call_result(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """The last check on a tool result before the model gets it.

    A result within its allowance is passed through untouched. A larger one is saved whole to a
    file, and the model gets a notice with the file's path and the start of the output; it reads
    the rest with Read.
    """
    values = {**(args or {}), **kwargs}
    limit_chars = _limit_chars(values)
    normalized = _normalize_outcome(values["outcome"], ERROR_ENVELOPE_BYTES)
    content = normalized["content"]
    original_bytes = utf8_byte_length(content)
    diagnostics = list(normalized["diagnostics"])
    limit = limit_chars if limit_chars is not None else ABSOLUTE_TOOL_RESULT_CEILING_BYTES
    over = len(content) > limit_chars if limit_chars is not None else original_bytes > limit
    if not over:
        return {
            "result": normalized["result"],
            "originalBytes": original_bytes,
            "finalBytes": original_bytes,
            "limitBytes": limit,
            "truncated": False,
            "artifactUnavailable": False,
            "diagnostics": diagnostics,
        }
    content_type = "json" if _looks_like_json(content) else "text"
    artifact, artifact_unavailable = await _try_artifact(values, content, content_type)
    if artifact_unavailable and values.get("requireArtifact"):
        return _required_artifact_failure(values, normalized, original_bytes, limit, diagnostics)
    shown = build_persisted_output(content, original_bytes, artifact.get("filePath") if artifact else None)
    final_bytes = utf8_byte_length(shown)
    diagnostics.append(_diagnostic(
        "artifact_unavailable" if artifact is None else "content_truncated",
        original_bytes, final_bytes, limit, artifact,
    ))
    result = {
        "result": {**normalized["result"], "content": shown},
        "originalBytes": original_bytes,
        "finalBytes": final_bytes,
        "limitBytes": limit,
        "truncated": True,
        "artifactUnavailable": artifact is None,
        "diagnostics": diagnostics,
    }
    if artifact:
        result["artifact"] = artifact
    return result


def serialized_content_bytes(content: str) -> int:
    return utf8_byte_length(_json_stringify(content))


def assert_tool_result_content_limit(content: str, maxBytes: int) -> None:
    assert_utf8_byte_limit(_json_stringify(content), maxBytes, "Serialized tool-result content")


def bounded_tool_identity(value: str) -> str:
    return slice_utf8_prefix(normalize_utf8_string(value), MAX_TOOL_IDENTITY_BYTES)


def _required_artifact_failure(
    values: dict[str, Any],
    normalized: dict[str, Any],
    original_bytes: int,
    limit_bytes: int,
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    content = _fit_envelope_with_excerpt(
        {
            "type": "tool_error",
            "code": "artifact_persistence_failed",
            "tool": bounded_tool_identity(values["toolName"]),
            "status": "completed_but_output_unavailable",
            "originalBytes": original_bytes,
            "outputAvailable": "unavailable",
            **(
                {"sideEffectsMayHaveOccurred": True}
                if values.get("sideEffectsMayHaveOccurred")
                else {}
            ),
        },
        "The tool completed, but Tess Core could not durably preserve its "
        "complete output. The result is withheld rather than silently losing data.",
        limit_bytes,
    )
    final_bytes = serialized_content_bytes(content)
    return {
        "result": {
            **normalized["result"],
            "content": content,
            "isError": True,
        },
        "originalBytes": original_bytes,
        "finalBytes": final_bytes,
        "limitBytes": limit_bytes,
        "truncated": True,
        "artifactUnavailable": True,
        "diagnostics": [
            *diagnostics,
            _diagnostic(
                "artifact_required_unavailable",
                original_bytes,
                final_bytes,
                limit_bytes,
                None,
            ),
        ],
    }


async def _try_artifact(
    values: dict[str, Any], content: str, content_type: str
) -> tuple[dict[str, Any] | None, bool]:
    if values["outcome"].get("kind") != "returned":
        return None, False
    writer: Callable[..., Any] = values.get("artifactWriter") or _persist_tool_output_artifact
    artifact_input = {
        "sessionId": values["sessionId"],
        "toolName": bounded_tool_identity(values["toolName"]),
        "family": "generic",
        "content": content,
        "contentType": content_type,
    }
    try:
        pending = writer(artifact_input)
        artifact = await pending if inspect.isawaitable(pending) else pending
        return artifact, False
    except Exception:
        return None, True


def _normalize_outcome(outcome: dict[str, Any], limit_bytes: int) -> dict[str, Any]:
    kind = outcome.get("kind")
    if kind == "thrown":
        content = _build_error_content("handler_threw", outcome.get("error"), limit_bytes)
        return {
            "result": {"content": content, "isError": True},
            "content": content,
            "diagnostics": [{
                "kind": "error_normalized",
                "originalBytes": _safe_error_bytes(outcome.get("error")),
                "finalBytes": serialized_content_bytes(content),
                "limitBytes": limit_bytes,
            }],
        }
    if kind == "synthesized":
        content = _safe_content_string(outcome.get("content"))
        result = {"content": content, "isError": bool(outcome.get("isError"))}
        if outcome.get("pause") == "approval":
            result["pause"] = "approval"
        return {"result": result, "content": content, "diagnostics": []}
    try:
        candidate = outcome.get("result")
        if not isinstance(candidate, dict) or not isinstance(candidate.get("content"), str):
            content = _build_error_content(
                "invalid_tool_result", "Tool handler returned no string content.", limit_bytes
            )
            return {
                "result": {"content": content, "isError": True},
                "content": content,
                "diagnostics": [{
                    "kind": "invalid_result", "originalBytes": 0,
                    "finalBytes": serialized_content_bytes(content), "limitBytes": limit_bytes,
                }],
            }
        content = normalize_utf8_string(candidate["content"])
        result: dict[str, Any] = {"content": content}
        if candidate.get("isError") is True:
            result["isError"] = True
        if candidate.get("pause") == "approval":
            result["pause"] = "approval"
        if isinstance(candidate.get("meta"), dict):
            result["meta"] = candidate["meta"]
        if isinstance(candidate.get("attachments"), list):
            result["attachments"] = candidate["attachments"]
        if "structuredContent" in candidate:
            result["structuredContent"] = candidate["structuredContent"]
        return {"result": result, "content": content, "diagnostics": []}
    except Exception as error:
        content = _build_error_content("result_serialization_failed", error, limit_bytes)
        return {
            "result": {"content": content, "isError": True},
            "content": content,
            "diagnostics": [{
                "kind": "invalid_result", "originalBytes": _safe_error_bytes(error),
                "finalBytes": serialized_content_bytes(content), "limitBytes": limit_bytes,
            }],
        }


def _build_error_content(code: str, error: Any, max_bytes: int) -> str:
    raw = slice_utf8_prefix(_safe_error_message(error), MAX_ERROR_MESSAGE_BYTES * 2)
    message = slice_utf8_prefix(
        _redact_sensitive_text(raw), min(MAX_ERROR_MESSAGE_BYTES, max(0, max_bytes - 512))
    )
    return _fit_envelope_with_excerpt({"type": "tool_error", "code": code}, message, max_bytes)


def _fit_envelope_with_excerpt(base: dict[str, Any], source: str, max_bytes: int) -> str:
    low = 0
    high = min(utf8_byte_length(source), max_bytes)
    best = _json_stringify(base)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = _json_stringify({**base, "excerpt": build_utf8_head_tail_sample(source, midpoint)})
        if serialized_content_bytes(candidate) <= max_bytes:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    if serialized_content_bytes(best) <= max_bytes:
        return best
    fallback = _json_stringify({"type": "tool_result_truncated", "outputTruncated": True})
    if serialized_content_bytes(fallback) <= max_bytes:
        return fallback
    return slice_utf8_prefix(fallback, max(0, max_bytes - 2))


def _safe_content_string(value: Any) -> str:
    return normalize_utf8_string(value) if isinstance(value, str) else "Tool result could not be represented safely."


def _safe_error_message(error: Any) -> str:
    try:
        if isinstance(error, BaseException):
            return f"{type(error).__name__}: {error}"
        if isinstance(error, str):
            return error
        return "Unknown tool failure"
    except Exception:
        return "Unknown tool failure"


def _safe_error_bytes(error: Any) -> int:
    return utf8_byte_length(_safe_error_message(error))


def _looks_like_json(value: str) -> bool:
    return value.lstrip().startswith(("{", "["))


def _diagnostic(
    kind: str, original: int, final: int, limit: int, artifact: dict[str, Any] | None
) -> dict[str, Any]:
    value = {"kind": kind, "originalBytes": original, "finalBytes": final, "limitBytes": limit}
    if artifact:
        value["artifactId"] = artifact.get("artifactId")
    return value


def _redact_sensitive_text(value: str) -> str:
    patterns = (
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
        re.compile(r"\b(access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret)\s*[:=]\s*[^\s,;]+", re.I),
    )
    for pattern in patterns:
        value = pattern.sub("[REDACTED]", value)
    return value


def _json_stringify(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


