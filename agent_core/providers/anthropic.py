from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..provider_errors import (
    canRetryProviderFailure,
    classifyProviderHttpFailure,
    createProviderFailureRecord,
    createProviderRequestError,
    normalizeUnknownProviderFailure,
    providerRetryDelayMs,
)


MESSAGES_API_URL = "https://api.anthropic.com/v1/messages?beta=true"
MESSAGES_COUNT_TOKENS_API_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_REQUEST_START_TIMEOUT_MS = 120_000
ANTHROPIC_STREAM_IDLE_TIMEOUT_MS = 60_000
ANTHROPIC_JSON_RESPONSE_TIMEOUT_MS = 120_000


def resolveAnthropicMessagesUrl(baseUrl: str | None) -> str:
    if not baseUrl:
        return MESSAGES_API_URL
    return f"{baseUrl.rstrip('/')}/messages?beta=true"


def resolveAnthropicCountTokensUrl(baseUrl: str | None) -> str:
    if not baseUrl:
        return MESSAGES_COUNT_TOKENS_API_URL
    return f"{baseUrl.rstrip('/')}/messages/count_tokens"


def buildAnthropicHeaders(config: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, str]:
    options = options or {}
    headers = {
        "accept": "text/event-stream" if options.get("stream") else "application/json",
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
    else:
        raise RuntimeError("No Anthropic credential is configured.")
    return headers


def buildAnthropicCountTokensPayload(request: dict[str, Any]) -> dict[str, Any]:
    payload = buildAnthropicMessagesPayload(request, {"stream": False})
    payload.pop("max_tokens", None)
    payload.pop("stream", None)
    payload.pop("output_config", None)
    return payload


def buildAnthropicMessagesPayload(request: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    payload: dict[str, Any] = {
        "model": request["model"],
        "max_tokens": 16384,
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=0.1.0; cc_entrypoint=tess; cch=00000;"},
            {"type": "text", "text": request.get("system", "")},
        ],
        "messages": [{"role": message.get("role"), "content": normalizeAnthropicContentBlocks(message.get("content"))} for message in request.get("messages", [])],
    }
    if options.get("stream"):
        payload["stream"] = True
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
        payload = json.loads(raw_event["data"])
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
    runtime = args.get("runtime") or {}
    max_attempts = max(1, int(args.get("maxAttempts") or 2))
    stream = bool(runtime.get("streamProviderMessages") or runtime.get("includePartialMessages"))
    timeout_ms = int(
        args.get("streamIdleTimeoutMs" if stream else "jsonResponseTimeoutMs")
        or args.get("requestStartTimeoutMs")
        or (ANTHROPIC_STREAM_IDLE_TIMEOUT_MS if stream else ANTHROPIC_JSON_RESPONSE_TIMEOUT_MS)
    )
    last_failure = None

    for attempt in range(1, max_attempts + 1):
        phase = "request_start"
        had_assistant_output = False
        had_tool_use = False
        try:
            _raise_anthropic_cancelled(runtime, phase, attempt, had_assistant_output, had_tool_use)
            runtime.get("emitEvent", lambda *_: None)(
                "provider.request_started",
                {"provider": "anthropic", "provider_label": "Anthropic", "model": request["model"], "attempt": attempt},
            )
            payload = buildAnthropicMessagesPayload(request, {"stream": stream})
            raw, content_type = await _race_anthropic_activity(
                asyncio.to_thread(_send_anthropic_messages_sync, config, payload, timeout_ms, stream),
                timeout_ms=timeout_ms,
                attempt=attempt,
                phase=phase,
                hadAssistantOutput=had_assistant_output,
                hadToolUse=had_tool_use,
                abortSignal=runtime.get("abortSignal"),
            )
            phase = "parse"
            if "text/event-stream" in content_type:
                response = normalizeAnthropicSseResponse(raw, request, runtime, attempt=attempt)
            else:
                parsed = json.loads(raw)
                response = {
                    "content": normalizeProviderBlocks(parsed.get("content", [])),
                    "usage": {
                        "input_tokens": (parsed.get("usage") or {}).get("input_tokens"),
                        "output_tokens": (parsed.get("usage") or {}).get("output_tokens"),
                        "cache_creation_input_tokens": (parsed.get("usage") or {}).get("cache_creation_input_tokens"),
                        "cache_read_input_tokens": (parsed.get("usage") or {}).get("cache_read_input_tokens"),
                    },
                    "model": parsed.get("model") or request["model"],
                    "stopReason": parsed.get("stop_reason"),
                }
            if attempt > 1:
                runtime.get("emitEvent", lambda *_: None)(
                    "provider.recovered",
                    {
                        "provider": "anthropic",
                        "provider_label": "Anthropic",
                        "model": request["model"],
                        "attempts": attempt,
                        "category": (last_failure.record if last_failure else {}).get("category"),
                    },
                )
            return response
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            parsed = _safe_json_parse(body) or {}
            error_payload = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            error_type = error_payload.get("type") if isinstance(error_payload.get("type"), str) else parsed.get("type") if isinstance(parsed.get("type"), str) else None
            failure = classifyProviderHttpFailure(
                {
                    "provider": "anthropic",
                    "providerLabel": "Anthropic",
                    "phase": phase,
                    "status": exc.code,
                    "attempt": attempt,
                    "hadAssistantOutput": had_assistant_output,
                    "hadToolUse": had_tool_use,
                    "body": body,
                    "errorType": error_type,
                }
            )
        except Exception as exc:
            failure = normalizeUnknownProviderFailure(
                {
                    "error": exc,
                    "provider": "anthropic",
                    "providerLabel": "Anthropic",
                    "phase": phase,
                    "attempt": attempt,
                    "hadAssistantOutput": had_assistant_output,
                    "hadToolUse": had_tool_use,
                }
            )
        last_failure = failure
        if canRetryProviderFailure(failure, max_attempts):
            delay_ms = providerRetryDelayMs(attempt)
            _raise_anthropic_cancelled(runtime, phase, attempt, had_assistant_output, had_tool_use)
            runtime.get("emitEvent", lambda *_: None)("provider.retrying", {**failure.record, "next_attempt": attempt + 1, "delay_ms": delay_ms})
            await asyncio.sleep(delay_ms / 1000)
            _raise_anthropic_cancelled(runtime, phase, attempt, had_assistant_output, had_tool_use)
            continue
        raise failure

    raise last_failure or RuntimeError("Anthropic request failed unexpectedly.")


def _send_anthropic_messages_sync(config: dict[str, Any], payload: dict[str, Any], timeout_ms: int, stream: bool = False) -> tuple[str, str]:
    req = Request(
        resolveAnthropicMessagesUrl(config.get("anthropicBaseUrl")),
        data=json.dumps(payload).encode("utf-8"),
        headers=buildAnthropicHeaders(config, {"stream": stream}),
        method="POST",
    )
    with urlopen(req, timeout=max(1, timeout_ms) / 1000) as response:
        raw = response.read().decode("utf-8")
        content_type = response.headers.get("content-type", "")
    return raw, content_type


async def _race_anthropic_activity(
    awaitable: Any,
    *,
    timeout_ms: int,
    attempt: int = 1,
    phase: str = "request_start",
    hadAssistantOutput: bool = False,
    hadToolUse: bool = False,
    abortSignal: Any = None,
) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=max(1, timeout_ms) / 1000)
    except asyncio.TimeoutError as exc:
        _raise_anthropic_cancelled({"abortSignal": abortSignal}, phase, attempt, hadAssistantOutput, hadToolUse)
        raise createProviderRequestError(
            createProviderFailureRecord(
                provider="anthropic",
                providerLabel="Anthropic",
                phase=phase,
                category="timeout",
                retryable=not hadAssistantOutput and not hadToolUse,
                attempt=attempt,
                hadAssistantOutput=hadAssistantOutput,
                hadToolUse=hadToolUse,
                rawMessage=f"Anthropic request timed out after {timeout_ms}ms while waiting for provider activity.",
            )
        ) from exc


def _raise_anthropic_cancelled(runtime: dict[str, Any], phase: str, attempt: int, hadAssistantOutput: bool, hadToolUse: bool) -> None:
    if not _signal_aborted(runtime.get("abortSignal")):
        return
    raise createProviderRequestError(
        createProviderFailureRecord(
            provider="anthropic",
            providerLabel="Anthropic",
            phase=phase,
            category="cancelled",
            retryable=False,
            attempt=attempt,
            hadAssistantOutput=hadAssistantOutput,
            hadToolUse=hadToolUse,
            rawMessage="Anthropic request was cancelled.",
        )
    )


def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))


def _build_anthropic_beta_header(config: dict[str, Any]) -> str:
    values = ["fine-grained-tool-streaming-2025-05-14"]
    if config.get("authToken"):
        values = ["claude-code-20250219", "oauth-2025-04-20", *values]
    return ",".join(dict.fromkeys(values))


def _apply_usage_snapshot(target: dict[str, Any], usage: dict[str, Any]) -> None:
    if not isinstance(usage, dict):
        return
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        if usage.get(key) is not None:
            target[key] = usage.get(key)


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


def _finalize_tool_block(block: dict[str, Any] | None) -> None:
    if not block or block.get("type") != "tool_use" or not str(block.get("_input_buffer") or "").strip():
        return
    block["input"] = json.loads(block["_input_buffer"])


def _strip_internal_block(block: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in block.items() if not key.startswith("_")}
