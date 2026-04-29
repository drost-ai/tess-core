from __future__ import annotations

from .shared import *

def parseAnthropicSseEvents(raw: str) -> list[dict[str, Any]]:
    normalized = raw.replace("\r\n", "\n")
    events = []
    for frame in normalized.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        event = None
        data_lines = []
        for line in frame.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        data = "\n".join(data_lines)
        if data:
            events.append({"event": event, "data": data})
    return events

def normalizeAnthropicSseResponse(raw: str, request: dict[str, Any], runtime: dict[str, Any] | None = None, attempt: int = 1) -> dict[str, Any]:
    runtime = runtime or {}
    blocks: list[dict[str, Any] | None] = []
    usage: dict[str, Any] = {}
    stop_reason = None
    model = request["model"]
    had_assistant_output = False
    had_tool_use = False
    for raw_event in parseAnthropicSseEvents(raw):
        if raw_event["data"] == "[DONE]":
            continue
        try:
            payload = json.loads(raw_event["data"])
        except Exception:
            raise _create_anthropic_protocol_error(
                f"Failed to parse streamed provider event: {raw_event['data']}",
                attempt,
                had_assistant_output,
                had_tool_use,
                raw_event.get("event"),
            )
        event_type = payload.get("type") or raw_event.get("event")
        if not event_type or event_type == "ping":
            continue
        if runtime.get("streamProviderMessages"):
            runtime.get("emitEvent", lambda *_: None)("provider.stream_event", {"stream_event": event_type, "model": model, "payload": payload})
        if event_type == "error":
            error = payload.get("error") or {}
            combined = f"{error.get('type') or ''} {error.get('message') or ''}".lower() if isinstance(error, dict) else json.dumps(payload).lower()
            category = "rate_limit" if re.search(r"rate|limit|throttle", combined) else "quota" if re.search(r"quota|billing|credit", combined) else "auth" if re.search(r"auth|unauthor|forbidden", combined) else "server"
            raise createProviderRequestError(
                createProviderFailureRecord(
                    provider="anthropic",
                    providerLabel="Anthropic",
                    phase="stream_read",
                    category=category,
                    retryable=category in {"rate_limit", "server"},
                    attempt=attempt,
                    hadAssistantOutput=had_assistant_output,
                    hadToolUse=had_tool_use,
                    rawMessage=json.dumps(payload),
                    errorType=error.get("type") if isinstance(error, dict) and isinstance(error.get("type"), str) else None,
                    streamEventType=event_type,
                )
            )
        if event_type == "message_start":
            message = payload.get("message") or {}
            model = message.get("model") or model
            _apply_usage_snapshot(usage, message.get("usage") or {})
        elif event_type == "content_block_start":
            index = payload.get("index")
            if not isinstance(index, int):
                continue
            while len(blocks) <= index:
                blocks.append(None)
            blocks[index] = _internal_block_from_anthropic(payload.get("content_block") or {})
            if blocks[index].get("type") == "tool_use":
                had_tool_use = True
            if blocks[index].get("type") == "text" and blocks[index].get("text"):
                had_assistant_output = True
        elif event_type == "content_block_delta":
            index = payload.get("index")
            if not isinstance(index, int) or index >= len(blocks) or blocks[index] is None:
                continue
            block = blocks[index]
            delta = payload.get("delta") or {}
            if block["type"] == "text" and delta.get("type") == "text_delta":
                text_delta = delta.get("text") or ""
                block["text"] += text_delta
                if text_delta:
                    had_assistant_output = True
                if runtime.get("includePartialMessages") and text_delta:
                    runtime.get("emitEvent", lambda *_: None)("message.assistant.partial", {"model": model, "text_delta": text_delta, "text": "".join(item.get("text", "") for item in blocks if item and item.get("type") == "text")})
            elif block["type"] == "tool_use" and delta.get("type") == "input_json_delta":
                block["_input_buffer"] = block.get("_input_buffer", "") + (delta.get("partial_json") or "")
        elif event_type == "content_block_stop":
            index = payload.get("index")
            if isinstance(index, int) and index < len(blocks):
                _finalize_tool_block(blocks[index], attempt=attempt, hadAssistantOutput=had_assistant_output, hadToolUse=had_tool_use)
        elif event_type == "message_delta":
            delta = payload.get("delta") or {}
            stop_reason = delta.get("stop_reason") or stop_reason
            _apply_usage_snapshot(usage, payload.get("usage") or {})
        elif event_type == "message_stop":
            break
    for block in blocks:
        _finalize_tool_block(block, attempt=attempt, hadAssistantOutput=had_assistant_output, hadToolUse=had_tool_use)
    return {"content": [_strip_internal_block(block) for block in blocks if block and block.get("type") in ("text", "tool_use")], "usage": usage, "model": model, "stopReason": stop_reason}

def normalizeProviderBlocks(blocks: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(blocks, list):
        return normalized
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            normalized.append({"type": "text", "text": block["text"]})
            continue
        if block.get("type") == "tool_use" and isinstance(block.get("id"), str) and isinstance(block.get("name"), str):
            normalized.append({"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input") if isinstance(block.get("input"), dict) else {}})
            continue
        if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
            normalized.append({"type": "tool_result", "tool_use_id": block["tool_use_id"], "content": block.get("content") if isinstance(block.get("content"), str) else "", "is_error": bool(block.get("is_error"))})
    return normalized

def _safe_json_parse(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None

def _internal_block_from_anthropic(block: dict[str, Any]) -> dict[str, Any]:
    if block.get("type") == "tool_use":
        return {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}, "_input_buffer": ""}
    if block.get("type") == "text":
        return {"type": "text", "text": block.get("text") or ""}
    return {"type": "ignored"}

def _finalize_tool_block(
    block: dict[str, Any] | None,
    *,
    attempt: int = 1,
    hadAssistantOutput: bool = False,
    hadToolUse: bool = False,
) -> None:
    if not block or block.get("type") != "tool_use" or not str(block.get("_input_buffer") or "").strip():
        return
    raw = str(block["_input_buffer"])
    try:
        parsed = json.loads(raw)
    except Exception:
        raise _create_anthropic_protocol_error(f"Unable to parse tool parameter JSON from model: {raw}", attempt, hadAssistantOutput, hadToolUse)
    if not isinstance(parsed, dict):
        raise _create_anthropic_protocol_error(f"Unable to parse tool parameter JSON from model: {raw}", attempt, hadAssistantOutput, hadToolUse)
    block["input"] = parsed

def _create_anthropic_protocol_error(rawMessage: str, attempt: int, hadAssistantOutput: bool, hadToolUse: bool, streamEventType: str | None = None) -> Any:
    return createProviderRequestError(
        createProviderFailureRecord(
            provider="anthropic",
            providerLabel="Anthropic",
            phase="parse",
            category="protocol",
            retryable=False,
            attempt=attempt,
            hadAssistantOutput=hadAssistantOutput,
            hadToolUse=hadToolUse,
            rawMessage=rawMessage,
            streamEventType=streamEventType,
        )
    )

def _strip_internal_block(block: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in block.items() if not key.startswith("_")}
