from __future__ import annotations

import copy
from typing import Any

STRICT_TOOL_PROTOCOL_PROVIDERS = {"anthropic", "openai"}


def normalizeProviderRequestMessages(provider: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return provider-safe request messages without mutating the canonical session.

    Anthropic and OpenAI both require tool results to directly answer the
    immediately preceding assistant tool_use blocks. Context relief and reactive
    retries may choose a suffix of the canonical session; this normalizes only
    the outbound request so the provider never sees a split tool exchange.
    """

    if str(provider or "").strip().lower() not in STRICT_TOOL_PROTOCOL_PROVIDERS:
        return {"messages": copy.deepcopy(messages), "repair": _empty_repair()}
    return _normalize_strict_tool_protocol(messages)


def _normalize_strict_tool_protocol(messages: list[dict[str, Any]]) -> dict[str, Any]:
    repaired: list[dict[str, Any]] = []
    expected_tool_ids: list[str] = []
    repair = _empty_repair()

    for raw_message in list(messages or []):
        message = copy.deepcopy(raw_message)
        role = str(message.get("role") or "")

        if role == "assistant":
            if expected_tool_ids:
                repaired.append(_missing_tool_results_message(expected_tool_ids))
                repair["syntheticToolResults"] += len(expected_tool_ids)
                expected_tool_ids = []
            repaired.append(message)
            expected_tool_ids = _tool_use_ids(message)
            continue

        if role == "user":
            normalized_user = _normalize_user_message(
                message,
                expected_tool_ids=expected_tool_ids,
                repair=repair,
            )
            repaired.extend(normalized_user["messages"])
            expected_tool_ids = []
            continue

        if expected_tool_ids:
            repaired.append(_missing_tool_results_message(expected_tool_ids))
            repair["syntheticToolResults"] += len(expected_tool_ids)
            expected_tool_ids = []
        repaired.append(message)

    if expected_tool_ids:
        repaired.append(_missing_tool_results_message(expected_tool_ids))
        repair["syntheticToolResults"] += len(expected_tool_ids)

    repair["changed"] = (
        repair["orphanedToolResults"] > 0
        or repair["syntheticToolResults"] > 0
        or len(repaired) != len(messages or [])
    )
    repair["messageCountBefore"] = len(messages or [])
    repair["messageCountAfter"] = len(repaired)
    return {"messages": repaired, "repair": repair}


def _normalize_user_message(
    message: dict[str, Any],
    *,
    expected_tool_ids: list[str],
    repair: dict[str, Any],
) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, list):
        if expected_tool_ids:
            repair["syntheticToolResults"] += len(expected_tool_ids)
            return {"messages": [_missing_tool_results_message(expected_tool_ids), message]}
        return {"messages": [message]}

    tool_result_blocks = [
        block
        for block in content
        if isinstance(block, dict) and str(block.get("type") or "") == "tool_result"
    ]
    if not tool_result_blocks:
        if expected_tool_ids:
            repair["syntheticToolResults"] += len(expected_tool_ids)
            return {"messages": [_missing_tool_results_message(expected_tool_ids), message]}
        return {"messages": [message]}

    if not expected_tool_ids:
        repair["orphanedToolResults"] += len(tool_result_blocks)
        return {"messages": [_historical_tool_results_message(content)]}

    expected = set(expected_tool_ids)
    valid_blocks: list[dict[str, Any]] = []
    orphan_blocks: list[dict[str, Any]] = []
    other_blocks: list[dict[str, Any]] = []
    seen_valid_ids: set[str] = set()

    for block in content:
        if not isinstance(block, dict) or str(block.get("type") or "") != "tool_result":
            other_blocks.append(_copy_non_tool_user_block(block))
            continue
        tool_use_id = str(block.get("tool_use_id") or "").strip()
        if tool_use_id in expected:
            valid_blocks.append(copy.deepcopy(block))
            seen_valid_ids.add(tool_use_id)
        else:
            orphan_blocks.append(copy.deepcopy(block))

    missing = [tool_id for tool_id in expected_tool_ids if tool_id not in seen_valid_ids]
    fixed_tool_blocks = [
        *valid_blocks,
        *[_synthetic_tool_result_block(tool_id) for tool_id in missing],
        *other_blocks,
    ]
    if missing:
        repair["syntheticToolResults"] += len(missing)

    out = [{**message, "content": fixed_tool_blocks}]
    if orphan_blocks:
        repair["orphanedToolResults"] += len(orphan_blocks)
        out.append(_historical_tool_results_message(orphan_blocks))
    return {"messages": out}


def _tool_use_ids(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    ids: list[str] = []
    for block in content:
        if not isinstance(block, dict) or str(block.get("type") or "") != "tool_use":
            continue
        tool_id = str(block.get("id") or "").strip()
        if tool_id:
            ids.append(tool_id)
    return ids


def _missing_tool_results_message(tool_ids: list[str]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [_synthetic_tool_result_block(tool_id) for tool_id in tool_ids],
    }


def _synthetic_tool_result_block(tool_id: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": str(tool_id),
        "is_error": True,
        "content": "Tool result omitted from retained request context.",
    }


def _historical_tool_results_message(blocks: list[Any]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": _historical_tool_results_text(blocks),
    }


def _historical_tool_results_text(blocks: list[Any]) -> str:
    parts = ["Historical tool context preserved from an earlier retained message:"]
    for block in blocks:
        if not isinstance(block, dict):
            text = str(block).strip()
            if text:
                parts.append(text)
            continue
        block_type = str(block.get("type") or "")
        if block_type == "text":
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
            continue
        if block_type != "tool_result":
            parts.append(str(block))
            continue
        tool_use_id = str(block.get("tool_use_id") or "").strip() or "unknown"
        status = "error" if block.get("is_error") else "ok"
        content = str(block.get("content") or "")
        parts.append(f"[tool_result id={tool_use_id} status={status}]\n{content}")
    return "\n\n".join(parts).strip()


def _copy_non_tool_user_block(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return copy.deepcopy(block)
    return {"type": "text", "text": str(block)}


def _empty_repair() -> dict[str, Any]:
    return {
        "changed": False,
        "orphanedToolResults": 0,
        "syntheticToolResults": 0,
        "messageCountBefore": 0,
        "messageCountAfter": 0,
    }
