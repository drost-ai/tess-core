from __future__ import annotations

from ..coerce import asRecord

from .shared import LOW_VALUE_TOOL_NAMES, _tool_result_content_string

import json
import re
from typing import Any
from ...payloads.image_redaction import redactImagePayloads as _redact_image_payloads
from ...payloads.todo_policy import selectTodoState as _select_todo_state


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




def classifyContextPressure(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args.get("session") or {}
    messages = args.get("messages") or []
    system_context = args.get("systemContext") or ""
    categories = {
        "oldHistoryTokens": ((session.get("metadata") or {}).get("compactionState") or {}).get("compactedContextTokenEstimate") or 0,
        "currentTurnToolPayloadTokens": 0,
        "lowValueTailTokens": 0,
        "repeatedFileContentTokens": 0,
        "planTodoNoiseTokens": estimateTextTokens(system_context) + estimateTodoAndPlanTokens(session),
    }

    tool_uses: dict[str, str] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_uses[str(block.get("id") or "")] = str(block.get("name") or "")
                continue
            if block.get("type") != "tool_result":
                continue
            content_text = _tool_result_content_string(block.get("content"))
            tool_name = tool_uses.get(str(block.get("tool_use_id") or ""), "UnknownTool")
            token_estimate = estimateTextTokens(content_text)
            categories["currentTurnToolPayloadTokens"] += token_estimate
            if tool_name in LOW_VALUE_TOOL_NAMES:
                categories["lowValueTailTokens"] += token_estimate
            if re.search(r'"content":|"filePath":|"startLine":|"totalLines":', content_text):
                categories["repeatedFileContentTokens"] += token_estimate

    return {
        "totalTokenEstimate": estimateMessagesTokens(messages) + estimateTextTokens(system_context),
        "categories": categories,
    }
