from __future__ import annotations

from typing import Any

"""The single tool-boundary encoding for session transcripts.

A tool exchange is an assistant message carrying tool_use blocks plus the
maximal run of consecutive user messages that follows it. The exchange is
complete when every tool_use id has a matching tool_result inside that run;
ids compare as str(id or "") on both sides, so an id-less tool_use pairs
only with an id-less tool_result. Every boundary question — save-time
repair, compaction cutoffs, continuation starts, the working-context
projection — must go through these predicates; encoding the rule a second
time is how transcripts diverged and got truncated.
"""


def messageToolUseIds(message: Any) -> list[str]:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [
        str(block.get("id") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]


def messageToolResultIds(message: Any) -> set[str]:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return set()
    return {
        str(block.get("tool_use_id") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }


def messageHasToolUses(message: Any) -> bool:
    return bool(messageToolUseIds(message))


def messageHasToolResults(message: Any) -> bool:
    return bool(messageToolResultIds(message))


def missingToolResultIds(messages: list[Any], index: int) -> list[str]:
    """Tool_use ids at messages[index] left unanswered within its exchange."""
    tool_ids = messageToolUseIds(messages[index])
    if not tool_ids:
        return []
    answered: set[str] = set()
    for message in messages[index + 1 :]:
        if not isinstance(message, dict) or message.get("role") != "user":
            break
        answered |= messageToolResultIds(message)
    return [tool_id for tool_id in tool_ids if tool_id not in answered]


def assistantToolUsesAreCompleted(messages: list[Any], index: int) -> bool:
    message = messages[index] if 0 <= index < len(messages) else None
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return False
    return not missingToolResultIds(messages, index)


def findIncompleteToolExchange(messages: list[Any]) -> dict[str, Any] | None:
    """First assistant message whose exchange is incomplete, or None."""
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        missing = missingToolResultIds(messages, index)
        if missing:
            return {"index": index, "missingToolUseIds": missing}
    return None


def sliceStartCrossesToolExchange(messages: list[Any], index: int) -> bool:
    """True when a slice starting at index begins inside a tool exchange:
    a tool_result at or past index answers a tool_use before index."""
    if index <= 0 or index >= len(messages):
        return False
    produced_before: set[str] = set()
    for message in messages[:index]:
        produced_before.update(messageToolUseIds(message))
    if not produced_before:
        return False
    produced_within: set[str] = set()
    for message in messages[index:]:
        produced_within.update(messageToolUseIds(message))
    return any(
        result_id in produced_before and result_id not in produced_within
        for message in messages[index:]
        for result_id in messageToolResultIds(message)
    )


def adjustStartToToolBoundary(messages: list[Any], candidate: int) -> int:
    """Largest start index <= candidate that does not split a tool exchange."""
    index = max(0, min(len(messages), candidate))
    while index > 0 and sliceStartCrossesToolExchange(messages, index):
        index -= 1
    return index
