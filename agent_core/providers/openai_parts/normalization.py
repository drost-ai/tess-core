from __future__ import annotations

from .shared import *

def _resolve_instructions(system: str, is_codex: bool) -> str:
    cleaned = system.strip()
    if not is_codex:
        return cleaned
    return f"{DEFAULT_CODEX_INSTRUCTIONS}\n\n{cleaned}" if cleaned else DEFAULT_CODEX_INSTRUCTIONS

def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "name": tool["name"], "description": tool["description"], "parameters": _normalize_openai_schema(tool.get("input_schema") or {})} for tool in tools]

def _convert_chat_completion_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": _normalize_openai_schema(tool.get("input_schema") or {})}} for tool in tools]

def _normalize_openai_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_normalize_openai_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    record = dict(schema)
    if record.get("type") == "array":
        record["items"] = _normalize_openai_schema(record.get("items") or {})
    if record.get("type") == "object" or "properties" in record:
        props = dict(record.get("properties") or {})
        record["properties"] = {name: _normalize_openai_schema(value) for name, value in props.items()}
    for key in ("items", "oneOf", "anyOf", "allOf", "if", "then", "else", "not"):
        if key in record:
            record[key] = _normalize_openai_schema(record[key])
    return record

def _extract_output_blocks(output: list[Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in output:
        if get_value(item, "type") == "message":
            for content in get_value(item, "content", []) or []:
                if get_value(content, "type") == "output_text" and isinstance(get_value(content, "text"), str):
                    blocks.append({"type": "text", "text": get_value(content, "text")})
        elif get_value(item, "type") == "function_call":
            call_id = str(get_value(item, "call_id", None) or get_value(item, "id", "") or "")
            name = str(get_value(item, "name", "") or "")
            if call_id and name:
                blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": _safe_json_object(str(get_value(item, "arguments", "") or ""))})
    return blocks

def _apply_openai_usage(usage: dict[str, Any], next_usage: Any) -> None:
    if get_value(next_usage, "input_tokens") is not None:
        usage["input_tokens"] = get_value(next_usage, "input_tokens")
    if get_value(next_usage, "output_tokens") is not None:
        usage["output_tokens"] = get_value(next_usage, "output_tokens")
    normalizeCacheUsageFields(usage, next_usage)

def _apply_chat_completion_usage(usage: dict[str, Any], next_usage: Any) -> None:
    if get_value(next_usage, "prompt_tokens") is not None:
        usage["input_tokens"] = get_value(next_usage, "prompt_tokens")
    if get_value(next_usage, "completion_tokens") is not None:
        usage["output_tokens"] = get_value(next_usage, "completion_tokens")
    normalizeCacheUsageFields(usage, next_usage)

def _emit_pending_chat_tool_calls(blocks: list[dict[str, Any]], pending: dict[int, dict[str, str]], emitted_ids: set[str], reasoning: str = "") -> bool:
    emitted = False
    for item in list(pending.values()):
        call_id = (item.get("id") or "").strip()
        name = (item.get("name") or "").strip()
        if not call_id or not name or call_id in emitted_ids:
            continue
        emitted_ids.add(call_id)
        block: dict[str, Any] = {"type": "tool_use", "id": call_id, "name": name, "input": _safe_json_object(item.get("arguments") or "")}
        if reasoning:
            block["provider_metadata"] = {"reasoning_content": reasoning}
        blocks.append(block)
        emitted = True
    if emitted:
        pending.clear()
    return emitted

def _extract_provider_reasoning_content(blocks: list[dict[str, Any]]) -> str | None:
    for block in blocks:
        if block.get("type") not in ("text", "tool_use"):
            continue
        reasoning = ((block.get("provider_metadata") or {}).get("reasoning_content") or "").strip()
        if reasoning:
            return reasoning
    return None

def _merge_tool_call_name(current: str | None, next_name: str) -> str:
    if not current:
        return next_name
    if next_name.startswith(current):
        return next_name
    if current.endswith(next_name):
        return current
    return f"{current}{next_name}"

def _append_text_block(blocks: list[dict[str, Any]], text: str) -> None:
    if not text:
        return
    if blocks and blocks[-1].get("type") == "text":
        blocks[-1]["text"] += text
    else:
        blocks.append({"type": "text", "text": text})

def _safe_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}

def _response_error_text(response: Any) -> str:
    error = get_value(response, "error", {}) or {}
    code = str(get_value(error, "code", "") or "")
    message = str(get_value(error, "message", "") or "")
    if code and message:
        return f"{code}: {message}"
    if message:
        return message
    details = get_value(response, "incomplete_details", {}) or {}
    reason = str(get_value(details, "reason", "") or "")
    return f"incomplete: {reason}" if reason else str(get_value(response, "status", "provider response failed"))
