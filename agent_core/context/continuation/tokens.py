from __future__ import annotations

from .shared import *
from ...todo_policy import selectTodoState as _select_todo_state


def estimateMessagesTokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        value = f"{message.get('role')}:{content if isinstance(content, str) else json.dumps(_redact_image_payloads(content), ensure_ascii=False, separators=(',', ':'))}"
        total += estimateTextTokens(value)
    return total


def estimateTodoAndPlanTokens(session: dict[str, Any]) -> int:
    selected = _select_todo_state(session.get("todos", []), {"maxOpen": 20, "maxCompleted": 8})
    todo_text = "\n".join(
        " ".join(str(part) for part in (todo.get("content"), todo.get("activeForm"), todo.get("status")) if part)
        for todo in [*selected["open"], *selected["completed"]]
    )
    plan_state = (session.get("metadata") or {}).get("planMode")
    plan_text = plan_state.get("plan") if isinstance(plan_state, dict) and isinstance(plan_state.get("plan"), str) else ""
    return estimateTextTokens(f"{todo_text}\n{plan_text}")


def estimateTextTokens(value: str) -> int:
    return (len(value) + 3) // 4


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def collapseWhitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def asRecord(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _redact_image_payloads(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_image_payloads(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    is_image = str(value.get("type") or "").strip().lower() in {"image", "input_image", "image_url"}
    for key, item in value.items():
        if is_image and key in {"data", "data_base64", "base64", "image_url", "url"}:
            out[key] = _image_placeholder(value)
        else:
            out[key] = _redact_image_payloads(item)
    return out


def _image_placeholder(value: dict[str, Any]) -> str:
    mime_type = value.get("mime_type") or value.get("mimeType") or value.get("mime") or "image"
    return f"<image:{mime_type}>"
