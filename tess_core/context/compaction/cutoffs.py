from __future__ import annotations

from .shared import (
    compaction_knob,
    LOW_VALUE_TAIL_TOOL_NAMES,
    MIN_PRESERVED_TOOL_EXCHANGE_PAIRS,
)
from .utils import estimate_message_tokens
from typing import Any
from ...storage.message_origin import is_plain_user_turn_anchor as _is_plain_user_turn_anchor
from ...session.tool_boundaries import (
    assistant_tool_uses_are_completed,
    message_has_tool_results,
    message_has_tool_uses,
    message_tool_result_ids as _message_tool_result_ids,
    missing_tool_result_ids as _missing_tool_result_ids,
)


def choose_compaction_cutoff(messages: list[dict[str, Any]], force: bool = False, previousCutoff: int = 0) -> int | None:
    if not messages:
        return None
    tool_aware_cutoff = choose_tool_aware_compaction_cutoff(messages, previousCutoff)
    if tool_aware_cutoff is not None and tool_aware_cutoff > previousCutoff:
        return tool_aware_cutoff

    fallback_index = max(1, len(messages) - compaction_knob("compaction.min_recent_messages"))
    index = len(messages)
    token_count = 0
    message_count = 0
    while index > 0 and (message_count < compaction_knob("compaction.min_recent_messages") or token_count < compaction_knob("compaction.raw_tail_token_target")):
        index -= 1
        token_count += estimate_message_tokens([messages[index]])
        message_count += 1
    if index == 0:
        index = fallback_index

    for cursor in range(index, 0, -1):
        if is_safe_working_set_start(messages, cursor):
            return cursor
    for cursor in range(fallback_index, 0, -1):
        if is_safe_working_set_start(messages, cursor):
            return cursor
    return None


def choose_tool_aware_compaction_cutoff(messages: list[dict[str, Any]], previousCutoff: int = 0) -> int | None:
    pair_starts = collect_substantive_tool_exchange_starts(messages, previousCutoff)
    if not pair_starts:
        return None
    preserved_starts = pair_starts[-min(compaction_knob("compaction.max_preserved_tool_pairs"), len(pair_starts)) :]
    while len(preserved_starts) > MIN_PRESERVED_TOOL_EXCHANGE_PAIRS:
        cutoff = preserved_starts[0]
        if estimate_message_tokens(messages[cutoff:]) <= compaction_knob("compaction.raw_tail_token_budget"):
            return cutoff
        preserved_starts = preserved_starts[1:]
    return preserved_starts[0]


def collect_substantive_tool_exchange_starts(messages: list[dict[str, Any]], previousCutoff: int = 0) -> list[int]:
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
        if not _missing_tool_result_ids(messages, message_index):
            starts.append(message_index)
    return starts


def collect_tool_result_ids(messages: list[dict[str, Any]]) -> set[str]:
    result_ids: set[str] = set()
    for message in messages:
        result_ids |= _message_tool_result_ids(message)
    return result_ids


def is_safe_working_set_start(messages: list[dict[str, Any]], index: int) -> bool:
    current = messages[index] if 0 <= index < len(messages) else None
    if not current:
        return False
    if _is_plain_user_turn_anchor(current):
        previous = messages[index - 1] if index > 0 else None
        return not (previous and previous.get("role") == "assistant" and message_has_tool_uses(previous))
    if current.get("role") != "assistant":
        return False
    if not message_has_tool_uses(current):
        return True
    return assistant_tool_uses_are_completed(messages, index)


# assistant_tool_uses_are_completed, message_has_tool_uses, and message_has_tool_results
# are re-exported from session.tool_boundaries — the single boundary encoding.
