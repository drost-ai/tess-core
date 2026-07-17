from __future__ import annotations

import base64
import binascii
import copy
import re
from typing import Any

STRICT_TOOL_PROTOCOL_PROVIDERS = {"anthropic", "openai"}
_IMAGE_PLACEHOLDER_RE = re.compile(r"^<image:(?P<mime>[^:>]+)(?::(?P<path>.*?))?:current-turn-only>$")


def normalizeProviderRequestMessages(provider: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return provider-safe request messages without mutating the canonical session.

    Anthropic and OpenAI both require tool results to directly answer the
    immediately preceding assistant tool_use blocks. Context relief and reactive
    retries may choose a suffix of the canonical session; this normalizes only
    the outbound request so the provider never sees a split tool exchange.
    """

    normalized = (
        _normalize_strict_tool_protocol(messages)
        if str(provider or "").strip().lower() in STRICT_TOOL_PROTOCOL_PROVIDERS
        else {"messages": copy.deepcopy(messages), "repair": _empty_repair()}
    )
    return _normalize_request_media(normalized)


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


def _normalize_request_media(normalized: dict[str, Any]) -> dict[str, Any]:
    repair = normalized.get("repair") if isinstance(normalized.get("repair"), dict) else _empty_repair()
    messages: list[dict[str, Any]] = []
    changed = False
    textified = 0

    for raw_message in normalized.get("messages") or []:
        message = copy.deepcopy(raw_message)
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            messages.append(message)
            continue
        next_content: list[Any] = []
        for block in content:
            if _is_unsendable_image_block(block):
                next_content.append(_image_replay_reference_block(block))
                textified += 1
                changed = True
            else:
                next_content.append(block)
        message["content"] = next_content
        messages.append(message)

    repair["imageBlocksTextified"] = int(repair.get("imageBlocksTextified") or 0) + textified
    repair["changed"] = bool(repair.get("changed")) or changed
    repair["messageCountBefore"] = len(normalized.get("messages") or [])
    repair["messageCountAfter"] = len(messages)
    return {"messages": messages, "repair": repair}


def _is_unsendable_image_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    block_type = str(block.get("type") or "").strip().lower()
    if block_type not in {"image", "input_image", "image_url"}:
        return False
    raw = _image_payload_string(block)
    if not raw:
        return True
    if _IMAGE_PLACEHOLDER_RE.match(raw):
        return True
    if raw.lower().startswith("data:"):
        return not _valid_data_url_image(raw)
    return not _valid_base64(raw)


def _image_payload_string(block: dict[str, Any]) -> str:
    source = block.get("source") if isinstance(block.get("source"), dict) else block
    value = source.get("data") or source.get("data_base64") or source.get("base64") or block.get("image_url") or block.get("url")
    if isinstance(value, dict):
        value = value.get("url")
    return str(value or "").strip()


def _valid_data_url_image(value: str) -> bool:
    prefix, marker, payload = value.partition(";base64,")
    if marker != ";base64,":
        return False
    if not prefix.lower().startswith("data:image/"):
        return False
    return _valid_base64(payload)


def _valid_base64(value: str) -> bool:
    raw = re.sub(r"\s+", "", value.strip())
    if not raw:
        return False
    try:
        base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def _image_replay_reference_block(block: Any) -> dict[str, str]:
    if not isinstance(block, dict):
        return {"type": "text", "text": "[Image attachment omitted from replay.]"}
    payload = _image_payload_string(block)
    match = _IMAGE_PLACEHOLDER_RE.match(payload)
    mime_type = str(block.get("mime_type") or block.get("mimeType") or block.get("mime") or "").strip()
    path = str(block.get("path") or block.get("file_path") or block.get("filePath") or block.get("source_path") or "").strip()
    if match:
        mime_type = mime_type or str(match.group("mime") or "").strip()
        path = path or str(match.group("path") or "").strip()
    label = path or "image attachment"
    suffix = f" ({mime_type})" if mime_type else ""
    return {
        "type": "text",
        "text": f"[Image attachment from an earlier turn: {label}{suffix}. Image bytes are current-turn-only and were not replayed.]",
    }


def _empty_repair() -> dict[str, Any]:
    return {
        "changed": False,
        "orphanedToolResults": 0,
        "syntheticToolResults": 0,
        "imageBlocksTextified": 0,
        "messageCountBefore": 0,
        "messageCountAfter": 0,
    }
