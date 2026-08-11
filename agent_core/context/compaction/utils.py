from __future__ import annotations

import re
from typing import Any
from ...payloads.token_estimation import (
    estimate_message_tokens,
    estimate_single_message_tokens,
    estimate_text_tokens,
)


def collapse_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def summarize_todo(todo: dict[str, Any]) -> str:
    content = collapse_whitespace(todo.get("content", ""))
    active_form = collapse_whitespace(todo.get("activeForm", ""))
    preferred = content if is_low_signal_todo_text(active_form) else active_form
    return truncate(preferred or content or active_form or "todo", 160)


def is_low_signal_todo_text(text: str) -> bool:
    return bool(re.match(r"^(coding|working|building|implementing|reviewing|testing|debugging|planning)$", text.strip(), re.IGNORECASE))
