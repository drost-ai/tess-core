from __future__ import annotations

import re
from typing import Any
from ...payloads.token_estimation import (
    estimateMessageTokens,
    estimateSingleMessageTokens,
    estimateTextTokens,
)


def _estimate_text_tokens(value: str) -> int:
    return estimateTextTokens(value)


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return estimateMessageTokens(messages)


def collapseWhitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def summarizeTodo(todo: dict[str, Any]) -> str:
    content = collapseWhitespace(todo.get("content", ""))
    active_form = collapseWhitespace(todo.get("activeForm", ""))
    preferred = content if isLowSignalTodoText(active_form) else active_form
    return truncate(preferred or content or active_form or "todo", 160)


def isLowSignalTodoText(text: str) -> bool:
    return bool(re.match(r"^(coding|working|building|implementing|reviewing|testing|debugging|planning)$", text.strip(), re.IGNORECASE))
