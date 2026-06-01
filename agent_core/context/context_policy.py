from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .continuation.tokens import estimateMessagesTokens, estimateTextTokens


RAW_CONTEXT_CHECKPOINT_TOKEN_LIMIT = 200_000


def estimateRawContextPressure(session: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now_dt = now or datetime.now(timezone.utc)
    messages = list(session.get("messages") or [])
    checkpoint = (session.get("metadata") or {}).get("compactionState") or {}
    checkpoint_message_count = _clamp_checkpoint_message_count(checkpoint.get("archiveMessageCount") or 0, len(messages))
    all_stats = _collect_message_stats(messages)
    delta_stats = _collect_message_stats(messages[checkpoint_message_count:])
    latest_compaction_at = checkpoint.get("latestCompactionAt") if isinstance(checkpoint.get("latestCompactionAt"), str) else None
    baseline_at = latest_compaction_at or session.get("createdAt")
    elapsed_ms_since_last_checkpoint = _elapsed_ms(now_dt, baseline_at)
    return {
        "rawTokenEstimate": all_stats["tokens"],
        "messageCount": len(messages),
        "assistantToolUseCount": all_stats["assistantToolUses"],
        "toolResultCount": all_stats["toolResults"],
        "messagesSinceLastCheckpoint": max(0, len(messages) - checkpoint_message_count),
        "toolResultsSinceLastCheckpoint": delta_stats["toolResults"],
        "rawTokenDeltaSinceLastCheckpoint": max(0, all_stats["tokens"] - _safe_int(checkpoint.get("rawTokenEstimate")))
        if checkpoint
        else all_stats["tokens"],
        "toolResultTokenEstimateSinceLastCheckpoint": delta_stats["toolResultTokens"],
        "largestToolResultTokenEstimate": delta_stats["largestToolResultTokens"],
        "elapsedMsSinceLastCheckpoint": elapsed_ms_since_last_checkpoint,
        "hasDurableCheckpoint": bool(checkpoint),
        "latestCompactionAt": latest_compaction_at,
    }


def decideDurableCheckpoint(session: dict[str, Any], pressure: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_context = pressure or estimateRawContextPressure(session)
    if not raw_context.get("hasDurableCheckpoint"):
        if _safe_int(raw_context.get("rawTokenEstimate")) >= RAW_CONTEXT_CHECKPOINT_TOKEN_LIMIT:
            return _checkpoint(
                "auto_raw_hard_limit",
                "raw session token estimate exceeded the durable checkpoint token limit",
                raw_context,
            )
    elif _safe_int(raw_context.get("rawTokenDeltaSinceLastCheckpoint")) >= RAW_CONTEXT_CHECKPOINT_TOKEN_LIMIT:
        return _checkpoint(
            "auto_raw_hard_limit",
            "raw session token delta since the last durable checkpoint exceeded the token limit",
            raw_context,
        )
    return {
        "shouldCheckpoint": False,
        "reason": "raw token delta since the last durable checkpoint is below the checkpoint token limit"
        if raw_context.get("hasDurableCheckpoint")
        else "raw session pressure is below the durable checkpoint token limit",
        "rawContext": raw_context,
    }


def _checkpoint(trigger: str, reason: str, raw_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "shouldCheckpoint": True,
        "trigger": trigger,
        "reason": reason,
        "rawContext": raw_context,
    }


def _collect_message_stats(messages: list[dict[str, Any]]) -> dict[str, int]:
    tokens = 0
    assistant_tool_uses = 0
    tool_results = 0
    tool_result_tokens = 0
    largest_tool_result_tokens = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        tokens += estimateMessagesTokens([message])
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if message.get("role") == "assistant" and block.get("type") == "tool_use":
                assistant_tool_uses += 1
                continue
            if block.get("type") != "tool_result":
                continue
            tool_results += 1
            result_tokens = estimateTextTokens(_tool_result_content_string(block.get("content")))
            tool_result_tokens += result_tokens
            largest_tool_result_tokens = max(largest_tool_result_tokens, result_tokens)
    return {
        "tokens": tokens,
        "assistantToolUses": assistant_tool_uses,
        "toolResults": tool_results,
        "toolResultTokens": tool_result_tokens,
        "largestToolResultTokens": largest_tool_result_tokens,
    }

def _tool_result_content_string(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def _clamp_checkpoint_message_count(value: Any, message_count: int) -> int:
    return max(0, min(message_count, _safe_int(value)))


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _elapsed_ms(now: datetime, value: Any) -> int | None:
    then = _parse_datetime(value)
    if then is None:
        return None
    return max(0, int((now - then).total_seconds() * 1000))


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
