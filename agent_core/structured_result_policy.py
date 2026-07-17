from __future__ import annotations

import json
from typing import Any

from .structured_value import summarizeStructuredValue
from .utf8_bounds import sliceUtf8Prefix, utf8ByteLength


STRUCTURED_RESULT_LIMITS = {
    "maxDepth": 5,
    "maxKeys": 40,
    "maxItems": 60,
    "maxStringBytes": 2_000,
    "maxSummaryBytes": 8_000,
}


def analyzeStructuredResult(content: str) -> dict[str, Any] | None:
    stripped = content.lstrip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = json.loads(content)
    except Exception:
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    declared = _parse_bounded_todo_summary_cardinality(parsed)
    if declared is not None:
        return {"parsed": parsed, "cardinality": declared, "requiresShaping": False}
    original = _count_structured_entries(parsed)
    summary = summarizeStructuredValue(parsed, STRUCTURED_RESULT_LIMITS)
    returned = min(original, summary["returnedItems"])
    truncated = bool(summary["truncated"])
    return {
        "parsed": parsed,
        "cardinality": {
            "originalItemCount": original,
            "returnedItemCount": returned if truncated else original,
            "omittedItemCount": original - returned if truncated else 0,
            "itemsTruncated": truncated,
        },
        "requiresShaping": truncated,
    }


def buildStructuredResultSummary(
    analysis: dict[str, Any] | None = None,
    maxBytes: int | None = None,
    artifact: dict[str, Any] | None = None,
    artifactUnavailable: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    if analysis is None and "args" in kwargs:
        values = kwargs["args"]
        analysis = values["analysis"]
        maxBytes = values["maxBytes"]
        artifact = values.get("artifact")
        artifactUnavailable = bool(values.get("artifactUnavailable"))
    assert analysis is not None and maxBytes is not None
    args = {
        "analysis": analysis,
        "maxBytes": maxBytes,
        "artifact": artifact,
        "artifactUnavailable": artifactUnavailable,
    }
    low = 0
    high = min(STRUCTURED_RESULT_LIMITS["maxSummaryBytes"], maxBytes)
    best = _build_candidate(args, 0)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = _build_candidate(args, midpoint)
        if _serialized_tool_content_bytes(candidate["content"]) <= maxBytes:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _build_candidate(args: dict[str, Any], max_summary_bytes: int) -> dict[str, Any]:
    summary = summarizeStructuredValue(
        args["analysis"]["parsed"],
        {**STRUCTURED_RESULT_LIMITS, "maxBytes": max_summary_bytes},
    )
    original = args["analysis"]["cardinality"]["originalItemCount"]
    returned = min(original, summary["returnedItems"])
    cardinality = {
        "originalItemCount": original,
        "returnedItemCount": returned,
        "omittedItemCount": original - returned,
        "itemsTruncated": True,
    }
    artifact = args.get("artifact")
    payload: dict[str, Any] = {
        "type": "structured_result_summary",
        **cardinality,
        "outputAvailable": "artifact" if artifact else "partial",
    }
    if artifact:
        payload["artifact"] = {
            "artifactId": artifact["artifactId"],
            "filePath": sliceUtf8Prefix(artifact["filePath"], 1_024),
            "bytes": artifact["bytes"],
            "contentType": artifact["contentType"],
        }
    if args.get("artifactUnavailable"):
        payload["artifactUnavailable"] = True
    payload["value"] = summary["value"]
    return {"content": _json_stringify(payload), "cardinality": cardinality}


def _count_structured_entries(root: Any) -> int:
    pending = [root]
    total = 0
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if not isinstance(value, (dict, list)):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(value, list):
            total += len(value)
            pending.extend(item for item in value if isinstance(item, (dict, list)))
        else:
            total += len(value)
            pending.extend(item for item in value.values() if isinstance(item, (dict, list)))
    return total


def _parse_bounded_todo_summary_cardinality(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("type") != "todo_state_summary":
        return None
    allowed = {
        "type", "updated", "originalItemCount", "returnedItemCount",
        "omittedItemCount", "itemsTruncated", "statusCounts", "todos",
    }
    todos = value.get("todos")
    if (
        not isinstance(todos, list)
        or len(todos) > 20
        or any(key not in allowed for key in value)
        or not _is_bounded_status_counts(value.get("statusCounts"))
        or not all(_is_bounded_todo_projection(todo) for todo in todos)
    ):
        return None
    original = _non_negative_safe_integer(value.get("originalItemCount"))
    returned = _non_negative_safe_integer(value.get("returnedItemCount"))
    omitted = _non_negative_safe_integer(value.get("omittedItemCount"))
    if (
        original is None or returned is None or omitted is None
        or value.get("updated") != original
        or returned != len(todos)
        or omitted != original - returned
        or value.get("itemsTruncated") is not (omitted > 0)
        or sum(value["statusCounts"].values()) != original
    ):
        return None
    return {
        "originalItemCount": original,
        "returnedItemCount": returned,
        "omittedItemCount": omitted,
        "itemsTruncated": omitted > 0,
    }


def _is_bounded_status_counts(value: Any) -> bool:
    keys = {"pending", "inProgress", "completed", "unknown"}
    return isinstance(value, dict) and set(value) == keys and all(
        _non_negative_safe_integer(value[key]) is not None for key in keys
    )


def _is_bounded_todo_projection(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= 4
        and _non_negative_safe_integer(value.get("position")) is not None
        and _is_bounded_text(value.get("content"), 2_000)
        and _is_bounded_text(value.get("activeForm"), 1_000)
        and value.get("status") in {"pending", "in_progress", "completed", "unknown"}
    )


def _is_bounded_text(value: Any, max_bytes: int) -> bool:
    return isinstance(value, str) and utf8ByteLength(value) <= max_bytes


def _non_negative_safe_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _serialized_tool_content_bytes(content: str) -> int:
    return utf8ByteLength(_json_stringify(content))


def _json_stringify(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


analyze_structured_result = analyzeStructuredResult
build_structured_result_summary = buildStructuredResultSummary
