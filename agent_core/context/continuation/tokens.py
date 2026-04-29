from __future__ import annotations

from .shared import *


def estimateMessagesTokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        value = f"{message.get('role')}:{content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, separators=(',', ':'))}"
        total += estimateTextTokens(value)
    return total


def estimateTodoAndPlanTokens(session: dict[str, Any]) -> int:
    todo_text = "\n".join(" ".join(str(part) for part in (todo.get("content"), todo.get("activeForm"), todo.get("status")) if part) for todo in session.get("todos", []))
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
