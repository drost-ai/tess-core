from __future__ import annotations

from .shared import (
    compaction_knob,
    LOW_VALUE_TAIL_TOOL_NAMES,
    MIN_PRESERVED_TOOL_EXCHANGE_PAIRS,
)
from .utils import estimateMessageTokens
from typing import Any
from ...session.message_origin import isPlainUserTurnAnchor as _is_plain_user_turn_anchor


def chooseCompactionCutoff(messages: list[dict[str, Any]], force: bool = False, previousCutoff: int = 0) -> int | None:
    if not messages:
        return None
    tool_aware_cutoff = chooseToolAwareCompactionCutoff(messages, previousCutoff)
    if tool_aware_cutoff is not None and tool_aware_cutoff > previousCutoff:
        return tool_aware_cutoff

    fallback_index = max(1, len(messages) - compaction_knob("compaction.min_recent_messages"))
    index = len(messages)
    token_count = 0
    message_count = 0
    while index > 0 and (message_count < compaction_knob("compaction.min_recent_messages") or token_count < compaction_knob("compaction.raw_tail_token_target")):
        index -= 1
        token_count += estimateMessageTokens([messages[index]])
        message_count += 1
    if index == 0:
        index = fallback_index

    for cursor in range(index, 0, -1):
        if isSafeWorkingSetStart(messages, cursor):
            return cursor
    for cursor in range(fallback_index, 0, -1):
        if isSafeWorkingSetStart(messages, cursor):
            return cursor
    return None


def chooseToolAwareCompactionCutoff(messages: list[dict[str, Any]], previousCutoff: int = 0) -> int | None:
    pair_starts = collectSubstantiveToolExchangeStarts(messages, previousCutoff)
    if not pair_starts:
        return None
    preserved_starts = pair_starts[-min(compaction_knob("compaction.max_preserved_tool_pairs"), len(pair_starts)) :]
    while len(preserved_starts) > MIN_PRESERVED_TOOL_EXCHANGE_PAIRS:
        cutoff = preserved_starts[0]
        if estimateMessageTokens(messages[cutoff:]) <= compaction_knob("compaction.raw_tail_token_budget"):
            return cutoff
        preserved_starts = preserved_starts[1:]
    return preserved_starts[0]


def collectSubstantiveToolExchangeStarts(messages: list[dict[str, Any]], previousCutoff: int = 0) -> list[int]:
    tool_results = collectToolResultIds(messages)
    starts: list[int] = []
    for message_index, message in enumerate(messages):
        if message_index <= previousCutoff or message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        tool_uses = [block for block in message["content"] if isinstance(block, dict) and block.get("type") == "tool_use"]
        if not tool_uses:
            continue
        has_substantive_tool = any(tool_use.get("name") not in LOW_VALUE_TAIL_TOOL_NAMES for tool_use in tool_uses)
        if not has_substantive_tool:
            continue
        if all(str(tool_use.get("id") or "") in tool_results for tool_use in tool_uses):
            starts.append(message_index)
    return starts


def collectToolResultIds(messages: list[dict[str, Any]]) -> set[str]:
    result_ids: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                result_ids.add(str(block.get("tool_use_id") or ""))
    return result_ids


def isSafeWorkingSetStart(messages: list[dict[str, Any]], index: int) -> bool:
    current = messages[index] if 0 <= index < len(messages) else None
    if not current:
        return False
    if _is_plain_user_turn_anchor(current):
        if messageHasToolResults(current):
            return False
        previous = messages[index - 1] if index > 0 else None
        return not (previous and previous.get("role") == "assistant" and messageHasToolUses(previous))
    if current.get("role") != "assistant":
        return False
    if not messageHasToolUses(current):
        return True
    return assistantToolUsesAreCompleted(messages, index)


def assistantToolUsesAreCompleted(messages: list[dict[str, Any]], index: int) -> bool:
    message = messages[index] if 0 <= index < len(messages) else None
    if not message or not isinstance(message.get("content"), list):
        return False
    tool_use_ids = [
        str(block.get("id") or "")
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if not tool_use_ids:
        return True
    result_ids = collectToolResultIds(messages[index + 1 :])
    return all(tool_id in result_ids for tool_id in tool_use_ids)


def messageHasToolUses(message: dict[str, Any]) -> bool:
    return isinstance(message.get("content"), list) and any(block.get("type") == "tool_use" for block in message["content"])


def messageHasToolResults(message: dict[str, Any]) -> bool:
    return isinstance(message.get("content"), list) and any(block.get("type") == "tool_result" for block in message["content"])
