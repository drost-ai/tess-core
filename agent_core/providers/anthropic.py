from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.request import Request, urlopen

from ..provider_errors import createProviderFailureRecord, createProviderRequestError


MESSAGES_API_URL = "https://api.anthropic.com/v1/messages?beta=true"
MESSAGES_COUNT_TOKENS_API_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_JSON_RESPONSE_TIMEOUT_MS = 120_000


def resolveAnthropicMessagesUrl(baseUrl: str | None) -> str:
    if not baseUrl:
        return MESSAGES_API_URL
    return f"{baseUrl.rstrip('/')}/messages?beta=true"


def resolveAnthropicCountTokensUrl(baseUrl: str | None) -> str:
    if not baseUrl:
        return MESSAGES_COUNT_TOKENS_API_URL
    return f"{baseUrl.rstrip('/')}/messages/count_tokens"


def buildAnthropicHeaders(config: dict[str, Any]) -> dict[str, str]:
    headers = {
        "accept": "text/event-stream",
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": _build_anthropic_beta_header(config),
        "anthropic-dangerous-direct-browser-access": "true",
        "user-agent": "tess/0.1.0 (sdk-cli)",
        "x-app": "cli",
        **(config.get("customHeaders") or {}),
    }
    if config.get("authToken"):
        headers["authorization"] = f"Bearer {config['authToken']}"
    elif config.get("apiKey"):
        headers["x-api-key"] = config["apiKey"]
    return headers


def buildAnthropicCountTokensPayload(request: dict[str, Any]) -> dict[str, Any]:
    payload = buildAnthropicMessagesPayload(request)
    payload.pop("max_tokens", None)
    payload.pop("stream", None)
    payload.pop("output_config", None)
    return payload


def buildAnthropicMessagesPayload(request: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request["model"],
        "max_tokens": 16384,
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=0.1.0; cc_entrypoint=tess; cch=00000;"},
            {"type": "text", "text": request.get("system", "")},
        ],
        "messages": [{"role": message.get("role"), "content": normalizeAnthropicContentBlocks(message.get("content"))} for message in request.get("messages", [])],
        "stream": True,
    }
    effort = request.get("effort")
    if effort:
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": effort}
    tools = request.get("tools") or []
    if tools:
        payload["tools"] = tools
    return payload


def normalizeAnthropicContentBlocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                out.append({"type": "text", "text": str(block.get("text", ""))})
            elif block.get("type") == "tool_use":
                out.append({"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}})
            elif block.get("type") == "tool_result":
                out.append({"type": "tool_result", "tool_use_id": block.get("tool_use_id"), "content": str(block.get("content", "")), "is_error": block.get("is_error")})
        return out
    return []


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


def normalizeAnthropicSseResponse(raw: str, request: dict[str, Any], runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = runtime or {}
    blocks: list[dict[str, Any] | None] = []
    usage: dict[str, Any] = {}
    stop_reason = None
    model = request["model"]
    for raw_event in parseAnthropicSseEvents(raw):
        if raw_event["data"] == "[DONE]":
            continue
        payload = json.loads(raw_event["data"])
        event_type = payload.get("type") or raw_event.get("event")
        if not event_type or event_type == "ping":
            continue
        if runtime.get("streamProviderMessages"):
            runtime.get("emitEvent", lambda *_: None)("provider.stream_event", {"stream_event": event_type, "model": model, "payload": payload})
        if event_type == "error":
            error = payload.get("error") or {}
            raise RuntimeError(json.dumps(error))
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
        elif event_type == "content_block_delta":
            index = payload.get("index")
            if not isinstance(index, int) or index >= len(blocks) or blocks[index] is None:
                continue
            block = blocks[index]
            delta = payload.get("delta") or {}
            if block["type"] == "text" and delta.get("type") == "text_delta":
                text_delta = delta.get("text") or ""
                block["text"] += text_delta
                if runtime.get("includePartialMessages") and text_delta:
                    runtime.get("emitEvent", lambda *_: None)("message.assistant.partial", {"model": model, "text_delta": text_delta, "text": "".join(item.get("text", "") for item in blocks if item and item.get("type") == "text")})
            elif block["type"] == "tool_use" and delta.get("type") == "input_json_delta":
                block["_input_buffer"] = block.get("_input_buffer", "") + (delta.get("partial_json") or "")
        elif event_type == "content_block_stop":
            index = payload.get("index")
            if isinstance(index, int) and index < len(blocks):
                _finalize_tool_block(blocks[index])
        elif event_type == "message_delta":
            delta = payload.get("delta") or {}
            stop_reason = delta.get("stop_reason") or stop_reason
            _apply_usage_snapshot(usage, payload.get("usage") or {})
        elif event_type == "message_stop":
            break
    for block in blocks:
        _finalize_tool_block(block)
    return {"content": [_strip_internal_block(block) for block in blocks if block and block.get("type") in ("text", "tool_use")], "usage": usage, "model": model, "stopReason": stop_reason}


async def sendAnthropicMessagesRequest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    config = args["config"]
    request = args["request"]
    payload = buildAnthropicMessagesPayload(request)
    timeout_ms = int(args.get("jsonResponseTimeoutMs") or args.get("requestStartTimeoutMs") or ANTHROPIC_JSON_RESPONSE_TIMEOUT_MS)
    raw, content_type = await _race_anthropic_activity(
        asyncio.to_thread(_send_anthropic_messages_sync, config, payload, timeout_ms),
        timeout_ms=timeout_ms,
    )
    if "text/event-stream" in content_type:
        return normalizeAnthropicSseResponse(raw, request, args.get("runtime") or {})
    parsed = json.loads(raw)
    return {
        "content": normalizeAnthropicContentBlocks(parsed.get("content", [])),
        "usage": {
            "input_tokens": (parsed.get("usage") or {}).get("input_tokens"),
            "output_tokens": (parsed.get("usage") or {}).get("output_tokens"),
            "cache_creation_input_tokens": (parsed.get("usage") or {}).get("cache_creation_input_tokens"),
            "cache_read_input_tokens": (parsed.get("usage") or {}).get("cache_read_input_tokens"),
        },
        "model": parsed.get("model") or request["model"],
        "stopReason": parsed.get("stop_reason"),
    }


def _send_anthropic_messages_sync(config: dict[str, Any], payload: dict[str, Any], timeout_ms: int) -> tuple[str, str]:
    req = Request(
        resolveAnthropicMessagesUrl(config.get("anthropicBaseUrl")),
        data=json.dumps(payload).encode("utf-8"),
        headers=buildAnthropicHeaders(config),
        method="POST",
    )
    with urlopen(req, timeout=max(1, timeout_ms) / 1000) as response:
        raw = response.read().decode("utf-8")
        content_type = response.headers.get("content-type", "")
    return raw, content_type


async def _race_anthropic_activity(awaitable: Any, *, timeout_ms: int) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=max(1, timeout_ms) / 1000)
    except asyncio.TimeoutError as exc:
        raise createProviderRequestError(
            createProviderFailureRecord(
                provider="anthropic",
                providerLabel="Anthropic",
                phase="request_start",
                category="timeout",
                retryable=True,
                attempt=1,
                hadAssistantOutput=False,
                hadToolUse=False,
                rawMessage=f"Anthropic request timed out after {timeout_ms}ms while waiting for provider activity.",
            )
        ) from exc


def _build_anthropic_beta_header(config: dict[str, Any]) -> str:
    values = ["fine-grained-tool-streaming-2025-05-14"]
    if config.get("authToken"):
        values = ["claude-code-20250219", "oauth-2025-04-20", *values]
    return ",".join(dict.fromkeys(values))


def _apply_usage_snapshot(target: dict[str, Any], usage: dict[str, Any]) -> None:
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        if usage.get(key) is not None:
            target[key] = usage.get(key)


def _internal_block_from_anthropic(block: dict[str, Any]) -> dict[str, Any]:
    if block.get("type") == "tool_use":
        return {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}, "_input_buffer": ""}
    if block.get("type") == "text":
        return {"type": "text", "text": block.get("text") or ""}
    return {"type": "ignored"}


def _finalize_tool_block(block: dict[str, Any] | None) -> None:
    if not block or block.get("type") != "tool_use" or not str(block.get("_input_buffer") or "").strip():
        return
    block["input"] = json.loads(block["_input_buffer"])


def _strip_internal_block(block: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in block.items() if not key.startswith("_")}
