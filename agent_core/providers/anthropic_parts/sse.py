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

class AnthropicSseAccumulator:
    def __init__(self, request: dict[str, Any], runtime: dict[str, Any] | None = None, attempt: int = 1) -> None:
        self.request = request
        self.runtime = runtime or {}
        self.attempt = attempt
        self.blocks: list[dict[str, Any] | None] = []
        self.usage: dict[str, Any] = {}
        self.stop_reason = None
        self.model = request["model"]
        self.had_assistant_output = False
        self.had_tool_use = False
        self.stopped = False

    def process_raw_event(self, raw_event: dict[str, Any]) -> None:
        if self.stopped:
            return
        if raw_event["data"] == "[DONE]":
            return
        try:
            payload = json.loads(raw_event["data"])
        except Exception:
            raise _create_anthropic_protocol_error(
                f"Failed to parse streamed provider event: {raw_event['data']}",
                self.attempt,
                self.had_assistant_output,
                self.had_tool_use,
                raw_event.get("event"),
            )
        event_type = payload.get("type") or raw_event.get("event")
        if not event_type or event_type == "ping":
            return
        if self.runtime.get("streamProviderMessages"):
            self.runtime.get("emitEvent", lambda *_: None)("provider.stream_event", {"stream_event": event_type, "model": self.model, "payload": payload})
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
                    attempt=self.attempt,
                    hadAssistantOutput=self.had_assistant_output,
                    hadToolUse=self.had_tool_use,
                    rawMessage=json.dumps(payload),
                    errorType=error.get("type") if isinstance(error, dict) and isinstance(error.get("type"), str) else None,
                    streamEventType=event_type,
                )
            )
        if event_type == "message_start":
            message = payload.get("message") or {}
            self.model = message.get("model") or self.model
            _apply_usage_snapshot(self.usage, message.get("usage") or {})
        elif event_type == "content_block_start":
            index = payload.get("index")
            if not isinstance(index, int):
                return
            while len(self.blocks) <= index:
                self.blocks.append(None)
            self.blocks[index] = _internal_block_from_anthropic(payload.get("content_block") or {})
            if self.blocks[index].get("type") == "tool_use":
                self.had_tool_use = True
            if self.blocks[index].get("type") == "text" and self.blocks[index].get("text"):
                self.had_assistant_output = True
        elif event_type == "content_block_delta":
            index = payload.get("index")
            if not isinstance(index, int) or index >= len(self.blocks) or self.blocks[index] is None:
                return
            block = self.blocks[index]
            delta = payload.get("delta") or {}
            if block["type"] == "text" and delta.get("type") == "text_delta":
                text_delta = delta.get("text") or ""
                block["text"] += text_delta
                if text_delta:
                    self.had_assistant_output = True
                if self.runtime.get("includePartialMessages") and text_delta:
                    self.runtime.get("emitEvent", lambda *_: None)("message.assistant.partial", {"model": self.model, "text_delta": text_delta, "text": "".join(item.get("text", "") for item in self.blocks if item and item.get("type") == "text")})
            elif block["type"] == "tool_use" and delta.get("type") == "input_json_delta":
                block["_input_buffer"] = block.get("_input_buffer", "") + (delta.get("partial_json") or "")
        elif event_type == "content_block_stop":
            index = payload.get("index")
            if isinstance(index, int) and index < len(self.blocks):
                _finalize_tool_block(self.blocks[index], attempt=self.attempt, hadAssistantOutput=self.had_assistant_output, hadToolUse=self.had_tool_use)
        elif event_type == "message_delta":
            delta = payload.get("delta") or {}
            self.stop_reason = delta.get("stop_reason") or self.stop_reason
            _apply_usage_snapshot(self.usage, payload.get("usage") or {})
        elif event_type == "message_stop":
            self.stopped = True

    def finalize(self) -> dict[str, Any]:
        for block in self.blocks:
            _finalize_tool_block(block, attempt=self.attempt, hadAssistantOutput=self.had_assistant_output, hadToolUse=self.had_tool_use)
        if not self.stopped:
            raise _create_anthropic_transport_error(
                "Anthropic SSE stream ended before message_stop.",
                self.attempt,
                self.had_assistant_output,
                self.had_tool_use,
                "eof",
            )
        return {
            "content": [_strip_internal_block(block) for block in self.blocks if block and block.get("type") in ("text", "tool_use")],
            "usage": self.usage,
            "model": self.model,
            "stopReason": self.stop_reason,
        }


def normalizeAnthropicSseResponse(raw: str, request: dict[str, Any], runtime: dict[str, Any] | None = None, attempt: int = 1) -> dict[str, Any]:
    accumulator = AnthropicSseAccumulator(request, runtime, attempt)
    for raw_event in parseAnthropicSseEvents(raw):
        accumulator.process_raw_event(raw_event)
    return accumulator.finalize()

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


def _create_anthropic_transport_error(rawMessage: str, attempt: int, hadAssistantOutput: bool, hadToolUse: bool, streamEventType: str | None = None) -> Any:
    return createProviderRequestError(
        createProviderFailureRecord(
            provider="anthropic",
            providerLabel="Anthropic",
            phase="stream_read",
            category="transport",
            retryable=True,
            attempt=attempt,
            hadAssistantOutput=hadAssistantOutput,
            hadToolUse=hadToolUse,
            rawMessage=rawMessage,
            streamEventType=streamEventType,
        )
    )


def _strip_internal_block(block: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in block.items() if not key.startswith("_")}
