from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

from .utf8_bounds import normalizeUtf8String, truncateUtf8, utf8ByteLength


TODO_WRITE_LIMITS = {
    "maxItems": 100,
    "maxResultItems": 20,
    "maxContentBytes": 2_000,
    "maxActiveFormBytes": 1_000,
    "maxViewContentBytes": 512,
    "maxViewActiveFormBytes": 256,
    "maxSerializedResultBytes": 12_000,
}


def requireTodoItems(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("Expected todos to be an array")
    if len(value) > TODO_WRITE_LIMITS["maxItems"]:
        raise ValueError(
            f"TodoWrite accepts at most {TODO_WRITE_LIMITS['maxItems']} items; received {len(value)}"
        )
    return [_require_todo_item(item, index) for index, item in enumerate(value)]


def deriveTodoActiveForm(content: str) -> str:
    normalized = content.strip().lower()
    if not normalized:
        return "work"
    match = re.match(r"^[a-z0-9]+", normalized, re.I)
    return match.group(0) if match else "work"


def selectTodoState(
    todos: Iterable[dict[str, Any]],
    limits: dict[str, Any],
) -> dict[str, Any]:
    values = list(todos)
    max_open = _normalize_projection_limit(limits.get("maxOpen"))
    max_completed = _normalize_projection_limit(limits.get("maxCompleted"))
    open_items: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    counts = {"pending": 0, "inProgress": 0, "completed": 0, "unknown": 0}
    for index, todo in enumerate(values):
        projection = _project_todo(todo, index)
        _increment_status_count(counts, projection["status"])
        if projection["status"] == "completed":
            _push_bounded_tail(completed, projection, max_completed)
        elif len(open_items) < max_open:
            open_items.append(projection)
    return {"total": len(values), "open": open_items, "completed": completed, "statusCounts": counts}


def summarizeTodoState(todos: Iterable[dict[str, Any]], maxItems: int = 20) -> dict[str, Any]:
    values = list(todos)
    limit = min(TODO_WRITE_LIMITS["maxResultItems"], _normalize_projection_limit(maxItems))
    selected = selectTodoState(values, {"maxOpen": limit, "maxCompleted": limit})
    completed_reserve = min(math.ceil(limit * 0.3), len(selected["completed"]))
    selected_open = selected["open"][: max(0, limit - completed_reserve)]
    selected_completed = selected["completed"][-max(0, limit - len(selected_open)) :] if limit - len(selected_open) > 0 else []
    representative = sorted(
        [*selected_open, *selected_completed], key=lambda item: item["position"]
    )[:limit]
    returned = len(representative)
    return {
        "type": "todo_state_summary",
        "updated": selected["total"],
        "originalItemCount": selected["total"],
        "returnedItemCount": returned,
        "omittedItemCount": selected["total"] - returned,
        "itemsTruncated": returned < selected["total"],
        "statusCounts": selected["statusCounts"],
        "todos": representative,
    }


def boundTodoViewText(value: Any, maxBytes: int = 512) -> str:
    return _bounded_legacy_text(value, maxBytes)


def serializeTodoStateSummary(todos: Iterable[dict[str, Any]], maxBytes: int = 12_000) -> str:
    summary = summarizeTodoState(todos)
    low = 0
    high = TODO_WRITE_LIMITS["maxViewContentBytes"]
    best = _build_fitted_todo_summary(summary, 0)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = _build_fitted_todo_summary(summary, midpoint)
        if _serialized_content_bytes(candidate) <= maxBytes:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    if _serialized_content_bytes(best) <= maxBytes:
        return best
    return _json_stringify(
        {
            **summary,
            "returnedItemCount": 0,
            "omittedItemCount": summary["originalItemCount"],
            "itemsTruncated": summary["originalItemCount"] > 0,
            "todos": [],
        }
    )


def updateActiveTaskFromTodos(session: dict[str, Any], todos: list[dict[str, str]]) -> None:
    active = next((todo for todo in todos if todo["status"] == "in_progress"), None)
    active = active or next((todo for todo in todos if todo["status"] == "pending"), None)
    metadata = session.setdefault("metadata", {})
    if active is None:
        metadata.pop("activeTask", None)
        return
    from .._utils import now_iso

    metadata["activeTask"] = {
        "updatedAt": now_iso(),
        "source": "todo",
        "content": active["content"],
        "activeForm": active["activeForm"],
    }


def _require_todo_item(value: Any, index: int) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid todo item at index {index}")
    content = _require_bounded_todo_text(
        value.get("content"), f"todos[{index}].content", TODO_WRITE_LIMITS["maxContentBytes"]
    )
    status = value.get("status")
    if status not in {"pending", "in_progress", "completed"}:
        raise ValueError(f"Invalid todos[{index}].status")
    raw_active = value.get("activeForm")
    active_form = (
        _require_bounded_todo_text(
            raw_active,
            f"todos[{index}].activeForm",
            TODO_WRITE_LIMITS["maxActiveFormBytes"],
        )
        if isinstance(raw_active, str) and raw_active.strip()
        else deriveTodoActiveForm(content)
    )
    return {"content": content, "status": status, "activeForm": active_form}


def _require_bounded_todo_text(value: Any, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Expected {field} to be a non-empty string")
    normalized = normalizeUtf8String(value)
    size = utf8ByteLength(normalized)
    if size > max_bytes:
        raise ValueError(f"{field} exceeds the {max_bytes}-byte UTF-8 limit ({size} bytes)")
    return normalized


def _project_todo(value: Any, index: int) -> dict[str, Any]:
    record = value if isinstance(value, dict) else {}
    content = _bounded_legacy_text(record.get("content"), TODO_WRITE_LIMITS["maxViewContentBytes"])
    active = _bounded_legacy_text(record.get("activeForm"), TODO_WRITE_LIMITS["maxViewActiveFormBytes"])
    status = record.get("status") if record.get("status") in {"pending", "in_progress", "completed"} else "unknown"
    return {
        "position": index + 1,
        "content": content,
        "status": status,
        "activeForm": active or deriveTodoActiveForm(content),
    }


def _bounded_legacy_text(value: Any, max_bytes: int) -> str:
    if not isinstance(value, str):
        return ""
    return truncateUtf8(value, max_bytes)["value"]


def _build_fitted_todo_summary(summary: dict[str, Any], content_bytes: int) -> str:
    active_bytes = min(TODO_WRITE_LIMITS["maxViewActiveFormBytes"], content_bytes // 2)
    return _json_stringify(
        {
            **summary,
            "todos": [
                {
                    **todo,
                    "content": truncateUtf8(todo["content"], content_bytes)["value"],
                    "activeForm": truncateUtf8(todo["activeForm"], active_bytes)["value"],
                }
                for todo in summary["todos"]
            ],
        }
    )


def _serialized_content_bytes(content: str) -> int:
    return utf8ByteLength(_json_stringify(content))


def _json_stringify(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _increment_status_count(counts: dict[str, int], status: str) -> None:
    counts["inProgress" if status == "in_progress" else status] += 1


def _push_bounded_tail(output: list[Any], value: Any, max_items: int) -> None:
    if max_items <= 0:
        return
    if len(output) == max_items:
        output.pop(0)
    output.append(value)


def _normalize_projection_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return 0
    return min(TODO_WRITE_LIMITS["maxItems"], math.floor(value))


require_todo_items = requireTodoItems
select_todo_state = selectTodoState
summarize_todo_state = summarizeTodoState
serialize_todo_state_summary = serializeTodoStateSummary
