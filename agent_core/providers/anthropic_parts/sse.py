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
        self.open_block_indexes: set[int] = set()

    def incomplete_response(self, reason: str, phase: str = "stream_read") -> Any:
        assistant_text = "".join(
            str(block.get("text") or block.get("thinking") or "")
            for block in self.blocks
            if block and block.get("type") in {"text", "thinking"}
        )
        return createProviderIncompleteResponseError(
            provider="anthropic",
            providerLabel="Anthropic",
            mode="messages",
            phase=phase,
            expectedTerminal="message_stop after every content block closed",
            reason=reason,
            attempt=self.attempt,
            hadAssistantOutput=self.had_assistant_output,
            hadToolUse=self.had_tool_use,
            partialAssistantBytes=len(assistant_text.encode("utf-8")),
            partialToolArgumentBytes=sum(
                len(str(block.get("_input_buffer") or "").encode("utf-8"))
                for block in self.blocks
                if block and block.get("type") == "tool_use"
            ),
            openItems=len(self.open_block_indexes),
        )

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
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise self.incomplete_response("a content block started without a valid index")
            if index in self.open_block_indexes or (
                index < len(self.blocks) and self.blocks[index] is not None
            ):
                raise self.incomplete_response(f"content block {index} started more than once")
            while len(self.blocks) <= index:
                self.blocks.append(None)
            content_block = payload.get("content_block")
            self.blocks[index] = _internal_block_from_anthropic(
                content_block if isinstance(content_block, dict) else {}
            )
            self.open_block_indexes.add(index)
            if self.blocks[index].get("type") == "tool_use":
                self.had_tool_use = True
            if self.blocks[index].get("type") == "text" and self.blocks[index].get("text"):
                self.had_assistant_output = True
        elif event_type == "content_block_delta":
            index = payload.get("index")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index not in self.open_block_indexes
            ):
                raise self.incomplete_response(
                    "a content block delta arrived without an open content block"
                )
            block = self.blocks[index]
            delta = payload.get("delta") or {}
            if not isinstance(block, dict) or not isinstance(delta, dict) or not isinstance(delta.get("type"), str):
                raise self.incomplete_response(f"content block {index} received a malformed delta")
            if block["type"] == "text" and delta.get("type") == "text_delta":
                text_delta = delta.get("text") if isinstance(delta.get("text"), str) else ""
                block["text"] += text_delta
                if text_delta:
                    self.had_assistant_output = True
                if self.runtime.get("includePartialMessages") and text_delta:
                    self.runtime.get("emitEvent", lambda *_: None)("message.assistant.partial", {"model": self.model, "text_delta": text_delta, "text": "".join(item.get("text", "") for item in self.blocks if item and item.get("type") == "text")})
            elif block["type"] == "thinking" and delta.get("type") == "thinking_delta":
                thinking_delta = delta.get("thinking") if isinstance(delta.get("thinking"), str) else ""
                block["thinking"] += thinking_delta
                if self.runtime.get("includePartialMessages") and thinking_delta:
                    self.runtime.get("emitEvent", lambda *_: None)(
                        "message.assistant.thinking.partial",
                        {
                            "model": self.model,
                            "thinking_delta": thinking_delta,
                            "thinking": "".join(
                                item.get("thinking", "")
                                for item in self.blocks
                                if item and item.get("type") == "thinking"
                            ),
                        },
                    )
            elif block["type"] == "thinking" and delta.get("type") == "signature_delta":
                signature_delta = delta.get("signature") if isinstance(delta.get("signature"), str) else ""
                if signature_delta:
                    block["signature"] = f"{block.get('signature') or ''}{signature_delta}"
            elif block["type"] == "tool_use" and delta.get("type") == "input_json_delta":
                partial_json = (
                    delta.get("partial_json")
                    if isinstance(delta.get("partial_json"), str)
                    else ""
                )
                block["_input_buffer"] = block.get("_input_buffer", "") + partial_json
        elif event_type == "content_block_stop":
            index = payload.get("index")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index not in self.open_block_indexes
            ):
                raise self.incomplete_response(
                    "a content block stopped without a matching start event"
                )
            try:
                _finalize_tool_block(self.blocks[index])
            except Exception as exc:
                raise self.incomplete_response(str(exc) or "tool arguments were malformed", "parse") from exc
            self.open_block_indexes.remove(index)
        elif event_type == "message_delta":
            delta = payload.get("delta") or {}
            self.stop_reason = delta.get("stop_reason") or self.stop_reason
            _apply_usage_snapshot(self.usage, payload.get("usage") or {})
        elif event_type == "message_stop":
            if self.open_block_indexes:
                raise self.incomplete_response(
                    "message_stop arrived while content blocks were still open"
                )
            self.stopped = True

    def finalize(self) -> dict[str, Any]:
        if not self.stopped:
            raise self.incomplete_response("the transport reached EOF before message_stop")
        content = [
            _strip_internal_block(block)
            for block in self.blocks
            if block and block.get("type") in ("text", "thinking", "tool_use")
        ]
        return {
            "content": content,
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
        if block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
            normalized.append(
                {
                    "type": "thinking",
                    "thinking": block["thinking"],
                    "signature": block.get("signature") if isinstance(block.get("signature"), str) else None,
                }
            )
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
    if (
        block.get("type") == "tool_use"
        and isinstance(block.get("id"), str)
        and isinstance(block.get("name"), str)
    ):
        return {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}, "_input_buffer": ""}
    if block.get("type") == "text":
        return {
            "type": "text",
            "text": block.get("text") if isinstance(block.get("text"), str) else "",
        }
    if block.get("type") == "thinking":
        return {
            "type": "thinking",
            "thinking": block.get("thinking") if isinstance(block.get("thinking"), str) else "",
            "signature": block.get("signature") if isinstance(block.get("signature"), str) else None,
        }
    return {"type": "ignored"}

def _finalize_tool_block(
    block: dict[str, Any] | None,
) -> None:
    if not block or block.get("type") != "tool_use" or not str(block.get("_input_buffer") or "").strip():
        return
    raw = str(block["_input_buffer"])
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise ValueError("Tool arguments ended before a complete JSON object was received.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must be a top-level JSON object.")
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
