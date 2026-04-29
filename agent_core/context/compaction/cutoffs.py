from __future__ import annotations

from .shared import *


def chooseCompactionCutoff(messages: list[dict[str, Any]], force: bool = False, previousCutoff: int = 0) -> int | None:
    if len(messages) < (MIN_RECENT_MESSAGE_COUNT + 2 if force else MIN_COMPACTABLE_MESSAGE_COUNT):
        return None
    tool_aware_cutoff = chooseToolAwareCompactionCutoff(messages, previousCutoff)
    if tool_aware_cutoff is not None and tool_aware_cutoff > previousCutoff:
        return tool_aware_cutoff

    fallback_index = max(1, len(messages) - MIN_RECENT_MESSAGE_COUNT)
    index = len(messages)
    token_count = 0
    message_count = 0
    while index > 0 and (message_count < MIN_RECENT_MESSAGE_COUNT or token_count < RAW_TAIL_TOKEN_TARGET):
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
    preserved_starts = pair_starts[-min(MAX_PRESERVED_TOOL_EXCHANGE_PAIRS, len(pair_starts)) :]
    while len(preserved_starts) > MIN_PRESERVED_TOOL_EXCHANGE_PAIRS:
        cutoff = preserved_starts[0]
        if estimateMessageTokens(messages[cutoff:]) <= RAW_TAIL_TOKEN_BUDGET:
            return cutoff
        preserved_starts = preserved_starts[1:]
    return preserved_starts[0]


def collectSubstantiveToolExchangeStarts(messages: list[dict[str, Any]], previousCutoff: int = 0) -> list[int]:
    tool_uses: dict[str, dict[str, Any]] = {}
    starts: set[int] = set()
    for message_index, message in enumerate(messages):
        if message_index < previousCutoff or not isinstance(message.get("content"), list):
            continue
        if message.get("role") == "assistant":
            for block in message["content"]:
                if block.get("type") == "tool_use":
                    tool_uses[str(block.get("id"))] = {"name": block.get("name"), "messageIndex": message_index}
            continue
        for block in message["content"]:
            if block.get("type") != "tool_result":
                continue
            tool_use = tool_uses.get(str(block.get("tool_use_id")))
            if not tool_use or tool_use.get("name") in LOW_VALUE_TAIL_TOOL_NAMES or int(tool_use.get("messageIndex") or 0) <= previousCutoff:
                continue
            starts.add(int(tool_use["messageIndex"]))
    return sorted(starts)


def isSafeWorkingSetStart(messages: list[dict[str, Any]], index: int) -> bool:
    current = messages[index] if 0 <= index < len(messages) else None
    if not current or current.get("role") != "user":
        return False
    if messageHasToolResults(current):
        return False
    previous = messages[index - 1] if index > 0 else None
    if previous and previous.get("role") == "assistant" and messageHasToolUses(previous):
        return False
    return True


def messageHasToolUses(message: dict[str, Any]) -> bool:
    return isinstance(message.get("content"), list) and any(block.get("type") == "tool_use" for block in message["content"])


def messageHasToolResults(message: dict[str, Any]) -> bool:
    return isinstance(message.get("content"), list) and any(block.get("type") == "tool_result" for block in message["content"])


async def resolveLatestPlan(session: dict[str, Any]) -> dict[str, Any] | None:
    approvals = [record for record in await listPlanApprovals() if record.get("sessionId") == session.get("id") or record.get("parentSessionId") == session.get("id")]
    approvals = sorted(approvals, key=lambda record: record.get("createdAt", ""), reverse=True)
    latest_approval = approvals[0] if approvals else None
    current_plan_state = session.get("metadata", {}).get("planMode")
    file_path = None
    if isinstance(current_plan_state, dict):
        file_path = current_plan_state.get("filePath")
    if not file_path and latest_approval:
        file_path = latest_approval.get("filePath")
    content = _read_text(file_path) if file_path else ""
    if not current_plan_state and not latest_approval and not file_path:
        return None
    request_id = (current_plan_state or {}).get("requestId") if isinstance(current_plan_state, dict) and "requestId" in current_plan_state else (latest_approval or {}).get("requestId")
    status = (current_plan_state or {}).get("state") if isinstance(current_plan_state, dict) and current_plan_state.get("state") is not None else (latest_approval or {}).get("status")
    return {
        "requestId": request_id,
        "status": status,
        "filePath": file_path,
        "exists": bool(file_path and content),
        "content": content or None,
    }
