from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from urllib.parse import urlparse

from .._utils import compact_json, get_value, maybe_await, to_async_iter
from ..provider_errors import (
    canRetryProviderFailure,
    createProviderFailureRecord,
    createProviderRequestError,
    normalizeUnknownProviderFailure,
    providerRetryDelayMs,
)


DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_INSTRUCTIONS = "You are a helpful and reliable assistant."
OPENAI_REQUEST_START_TIMEOUT_MS = 120_000
OPENAI_STREAM_IDLE_TIMEOUT_MS = 60_000
OPENAI_ABORT_POLL_INTERVAL_MS = 50


async def sendOpenAiRequest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    if args["mode"] == "chat_completions":
        return await sendOpenAiChatCompletionsRequest(args)
    return await sendOpenAiResponsesRequest(args)


async def sendOpenAiResponsesRequest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    mode = args.get("mode") or _infer_openai_responses_mode(args.get("baseUrl"))
    base_url = resolveOpenAiBaseUrl(mode, args.get("baseUrl"))
    provider_label = getOpenAiProviderLabel(mode, base_url)
    max_attempts = max(1, int(args.get("maxAttempts") or 2))
    last_failure = None
    for attempt in range(1, max_attempts + 1):
        phase = "request_start"
        had_output = False
        had_tool_use = False
        blocks: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        stop_reason = "stop"
        emitted_text = ""
        emitted_tool_ids: set[str] = set()
        pending_by_item_id: dict[str, dict[str, str]] = {}
        item_to_call_id: dict[str, str] = {}
        runtime = args.get("runtime") or {}
        request_start_timeout_ms = int(args.get("requestStartTimeoutMs") or OPENAI_REQUEST_START_TIMEOUT_MS)
        stream_idle_timeout_ms = int(args.get("streamIdleTimeoutMs") or OPENAI_STREAM_IDLE_TIMEOUT_MS)
        try:
            _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
            _emit(runtime, "provider.request_started", {"provider": "openai", "provider_label": provider_label, "model": args["request"]["model"], "attempt": attempt})
            client = _create_openai_client(args, base_url)
            responses = get_value(client, "responses")
            if not responses:
                raise RuntimeError("OpenAI Responses client is unavailable.")
            params = buildResponsesCreateParams({"isCodexBackend": mode == "codex", "request": args["request"]})
            response = await _race_openai_activity(
                maybe_await(get_value(responses, "create")(params)),
                timeout_ms=request_start_timeout_ms,
                provider_label=provider_label,
                phase=phase,
                attempt=attempt,
                had_output=had_output,
                had_tool_use=had_tool_use,
                abort_signal=runtime.get("abortSignal"),
            )
            phase = "stream_open"
            iterator = to_async_iter(response).__aiter__()
            while True:
                try:
                    _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                    event = await _race_openai_activity(
                        iterator.__anext__(),
                        timeout_ms=stream_idle_timeout_ms,
                        provider_label=provider_label,
                        phase=phase,
                        attempt=attempt,
                        had_output=had_output,
                        had_tool_use=had_tool_use,
                        abort_signal=runtime.get("abortSignal"),
                    )
                except StopAsyncIteration:
                    break
                phase = "stream_read"
                typ = str(get_value(event, "type", "") or "")
                if runtime.get("streamProviderMessages"):
                    _emit(runtime, "provider.stream_event", {"stream_event": typ, "model": args["request"]["model"], "payload": event})
                if typ == "response.output_text.delta":
                    delta = str(get_value(event, "delta", "") or "")
                    if not delta:
                        continue
                    had_output = True
                    emitted_text += delta
                    if runtime.get("includePartialMessages"):
                        _emit(runtime, "message.assistant.partial", {"session_id": None, "model": args["request"]["model"], "text_delta": delta, "text": emitted_text})
                    _append_text_block(blocks, delta)
                    continue
                if typ == "response.output_item.added":
                    item = get_value(event, "item", {})
                    if get_value(item, "type") == "function_call":
                        item_id = str(get_value(item, "id", "") or "")
                        call_id = str(get_value(item, "call_id", "") or "")
                        if item_id and call_id:
                            item_to_call_id[item_id] = call_id
                            pending = pending_by_item_id.get(item_id)
                            if pending and call_id not in emitted_tool_ids:
                                emitted_tool_ids.add(call_id)
                                had_tool_use = True
                                blocks.append({"type": "tool_use", "id": call_id, "name": pending["name"], "input": _safe_json_object(pending["raw"])})
                    continue
                if typ == "response.function_call_arguments.done":
                    item_id = str(get_value(event, "item_id", "") or "")
                    name = str(get_value(event, "name", "") or "")
                    raw = str(get_value(event, "arguments", "") or "")
                    if not item_id or not name:
                        continue
                    pending_by_item_id[item_id] = {"name": name, "raw": raw}
                    call_id = item_to_call_id.get(item_id)
                    if call_id and call_id not in emitted_tool_ids:
                        emitted_tool_ids.add(call_id)
                        had_tool_use = True
                        blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": _safe_json_object(raw)})
                    continue
                if typ == "response.output_item.done":
                    item = get_value(event, "item", {})
                    if get_value(item, "type") == "function_call":
                        call_id = str(get_value(item, "call_id", None) or get_value(item, "id", "") or "")
                        name = str(get_value(item, "name", "") or "")
                        if call_id and name and call_id not in emitted_tool_ids:
                            emitted_tool_ids.add(call_id)
                            had_tool_use = True
                            blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": _safe_json_object(str(get_value(item, "arguments", "") or ""))})
                    continue
                if typ == "response.completed":
                    phase = "response_complete"
                    completed = get_value(event, "response", {}) or {}
                    for block in _extract_output_blocks(get_value(completed, "output", []) or []):
                        if block["type"] == "text":
                            final_text = block["text"]
                            if final_text.startswith(emitted_text) and len(final_text) > len(emitted_text):
                                _append_text_block(blocks, final_text[len(emitted_text):])
                                emitted_text = final_text
                                had_output = True
                            elif not emitted_text and final_text:
                                _append_text_block(blocks, final_text)
                                emitted_text = final_text
                                had_output = True
                        elif block["id"] not in emitted_tool_ids:
                            emitted_tool_ids.add(block["id"])
                            had_tool_use = True
                            blocks.append(block)
                    _apply_openai_usage(usage, get_value(completed, "usage"))
                    stop_reason = "tool_calls" if any(block["type"] == "tool_use" for block in blocks) else "stop"
                    continue
                if typ == "response.failed":
                    raise createProviderRequestError(
                        createProviderFailureRecord(
                            provider="openai",
                            providerLabel=provider_label,
                            phase=phase,
                            category="server",
                            retryable=True,
                            attempt=attempt,
                            hadAssistantOutput=had_output,
                            hadToolUse=had_tool_use,
                            rawMessage=_response_error_text(get_value(event, "response")),
                            streamEventType=typ,
                        )
                    )
                if typ == "response.incomplete":
                    raise createProviderRequestError(
                        createProviderFailureRecord(
                            provider="openai",
                            providerLabel=provider_label,
                            phase=phase,
                            category="protocol",
                            retryable=False,
                            attempt=attempt,
                            hadAssistantOutput=had_output,
                            hadToolUse=had_tool_use,
                            rawMessage=_response_error_text(get_value(event, "response")),
                            streamEventType=typ,
                        )
                    )
                if typ == "error":
                    message = str(get_value(event, "message", "") or "")
                    code = str(get_value(event, "code", "") or "")
                    combined = f"{code} {message}".lower()
                    category = "rate_limit" if "rate" in combined or "limit" in combined else "auth" if "auth" in combined or "unauthor" in combined or "forbidden" in combined else "server"
                    raise createProviderRequestError(
                        createProviderFailureRecord(
                            provider="openai",
                            providerLabel=provider_label,
                            phase=phase,
                            category=category,
                            retryable=category in ("rate_limit", "server"),
                            attempt=attempt,
                            hadAssistantOutput=had_output,
                            hadToolUse=had_tool_use,
                            rawMessage=f"{code}: {message}" if code and message else message or "OpenAI stream error",
                            code=code or None,
                            streamEventType=typ,
                        )
                    )
            if attempt > 1:
                _emit(runtime, "provider.recovered", {"provider": "openai", "provider_label": provider_label, "model": args["request"]["model"], "attempts": attempt, "category": last_failure.record.get("category") if last_failure else None})
            return {"content": blocks, "usage": usage, "model": args["request"]["model"], "stopReason": stop_reason}
        except Exception as exc:
            failure = normalizeUnknownProviderFailure(
                error=exc,
                provider="openai",
                providerLabel=provider_label,
                phase=phase,
                attempt=attempt,
                hadAssistantOutput=had_output,
                hadToolUse=had_tool_use,
            )
            last_failure = failure
            if canRetryProviderFailure(failure, max_attempts):
                delay_ms = providerRetryDelayMs(attempt)
                _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                _emit(runtime, "provider.retrying", {**failure.record, "next_attempt": attempt + 1, "delay_ms": delay_ms})
                await asyncio.sleep(delay_ms / 1000)
                _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                continue
            raise failure
    raise last_failure or RuntimeError("OpenAI request failed unexpectedly.")


async def sendOpenAiChatCompletionsRequest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    base_url = resolveOpenAiBaseUrl("chat_completions", args.get("baseUrl"))
    provider_label = getOpenAiProviderLabel("chat_completions", base_url)
    max_attempts = max(1, int(args.get("maxAttempts") or 2))
    last_failure = None
    for attempt in range(1, max_attempts + 1):
        phase = "request_start"
        had_output = False
        had_tool_use = False
        blocks: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        stop_reason = "stop"
        emitted_text = ""
        emitted_tool_ids: set[str] = set()
        pending_tool_calls: dict[int, dict[str, str]] = {}
        reasoning_content = ""
        runtime = args.get("runtime") or {}
        request_start_timeout_ms = int(args.get("requestStartTimeoutMs") or OPENAI_REQUEST_START_TIMEOUT_MS)
        stream_idle_timeout_ms = int(args.get("streamIdleTimeoutMs") or OPENAI_STREAM_IDLE_TIMEOUT_MS)
        try:
            _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
            _emit(runtime, "provider.request_started", {"provider": "openai", "provider_label": provider_label, "model": args["request"]["model"], "attempt": attempt})
            client = _create_openai_client(args, base_url)
            chat = get_value(client, "chat")
            completions = get_value(chat, "completions") if chat else None
            if not completions:
                raise RuntimeError("OpenAI Chat Completions client is unavailable.")
            params = buildChatCompletionsCreateParams(args["request"], {"includeReasoningEffort": supportsOpenAiChatCompletionsReasoningEffort(base_url)})
            response = await _race_openai_activity(
                maybe_await(get_value(completions, "create")(params)),
                timeout_ms=request_start_timeout_ms,
                provider_label=provider_label,
                phase=phase,
                attempt=attempt,
                had_output=had_output,
                had_tool_use=had_tool_use,
                abort_signal=runtime.get("abortSignal"),
            )
            phase = "stream_open"
            iterator = to_async_iter(response).__aiter__()
            while True:
                try:
                    _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                    chunk = await _race_openai_activity(
                        iterator.__anext__(),
                        timeout_ms=stream_idle_timeout_ms,
                        provider_label=provider_label,
                        phase=phase,
                        attempt=attempt,
                        had_output=had_output,
                        had_tool_use=had_tool_use,
                        abort_signal=runtime.get("abortSignal"),
                    )
                except StopAsyncIteration:
                    break
                phase = "stream_read"
                if runtime.get("streamProviderMessages"):
                    _emit(runtime, "provider.stream_event", {"stream_event": "chat.completion.chunk", "model": args["request"]["model"], "payload": chunk})
                _apply_chat_completion_usage(usage, get_value(chunk, "usage"))
                for choice in get_value(chunk, "choices", []) or []:
                    delta = get_value(choice, "delta", {}) or {}
                    content_delta = get_value(delta, "content", "")
                    if isinstance(content_delta, str) and content_delta:
                        had_output = True
                        emitted_text += content_delta
                        if runtime.get("includePartialMessages"):
                            _emit(runtime, "message.assistant.partial", {"session_id": None, "model": args["request"]["model"], "text_delta": content_delta, "text": emitted_text})
                        _append_text_block(blocks, content_delta)
                    reasoning_delta = get_value(delta, "reasoning_content", "")
                    if isinstance(reasoning_delta, str) and reasoning_delta:
                        reasoning_content += reasoning_delta
                    tool_calls = get_value(delta, "tool_calls", []) or []
                    for default_index, tool_call in enumerate(tool_calls):
                        raw_index = get_value(tool_call, "index")
                        index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else default_index
                        pending = pending_tool_calls.setdefault(index, {"arguments": ""})
                        tool_call_id = get_value(tool_call, "id")
                        if isinstance(tool_call_id, str) and tool_call_id:
                            pending["id"] = tool_call_id
                        fn = get_value(tool_call, "function", {}) or {}
                        fn_name = get_value(fn, "name")
                        if isinstance(fn_name, str) and fn_name:
                            pending["name"] = _merge_tool_call_name(pending.get("name"), fn_name)
                        fn_arguments = get_value(fn, "arguments")
                        if isinstance(fn_arguments, str) and fn_arguments:
                            pending["arguments"] += fn_arguments
                    finish_value = get_value(choice, "finish_reason", "")
                    finish = finish_value if isinstance(finish_value, str) else ""
                    if finish in ("tool_calls", "function_call"):
                        had_tool_use = _emit_pending_chat_tool_calls(blocks, pending_tool_calls, emitted_tool_ids, reasoning_content) or had_tool_use
                        stop_reason = "tool_calls"
                    elif finish:
                        stop_reason = finish
            had_tool_use = _emit_pending_chat_tool_calls(blocks, pending_tool_calls, emitted_tool_ids, reasoning_content) or had_tool_use
            if attempt > 1:
                _emit(runtime, "provider.recovered", {"provider": "openai", "provider_label": provider_label, "model": args["request"]["model"], "attempts": attempt, "category": last_failure.record.get("category") if last_failure else None})
            return {"content": blocks, "usage": usage, "model": args["request"]["model"], "stopReason": stop_reason}
        except Exception as exc:
            failure = normalizeUnknownProviderFailure(
                error=exc,
                provider="openai",
                providerLabel=provider_label,
                phase=phase,
                attempt=attempt,
                hadAssistantOutput=had_output,
                hadToolUse=had_tool_use,
            )
            last_failure = failure
            if canRetryProviderFailure(failure, max_attempts):
                delay_ms = providerRetryDelayMs(attempt)
                _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                _emit(runtime, "provider.retrying", {**failure.record, "next_attempt": attempt + 1, "delay_ms": delay_ms})
                await asyncio.sleep(delay_ms / 1000)
                _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
                continue
            raise failure
    raise last_failure or RuntimeError("OpenAI Chat Completions request failed unexpectedly.")


def buildResponsesCreateParams(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    request = args["request"]
    is_codex = bool(args.get("isCodexBackend"))
    params: dict[str, Any] = {
        "model": request["model"],
        "input": convertMessagesToInput(request.get("messages", [])),
        "instructions": _resolve_instructions(request.get("system", ""), is_codex),
        "store": False,
        "stream": True,
    }
    if request.get("effort"):
        params["reasoning"] = {"effort": request["effort"]}
    if not is_codex:
        params["max_output_tokens"] = 16384
    tools = _convert_tools(request.get("tools", []))
    if tools:
        params["tools"] = tools
    return params


def buildChatCompletionsCreateParams(request: dict[str, Any] | None = None, options: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    request = {**(request or {}), **kwargs}
    options = options or {}
    params: dict[str, Any] = {
        "model": request["model"],
        "messages": convertMessagesToChatCompletions(request.get("system", ""), request.get("messages", [])),
        "max_tokens": 16384,
        "stream": True,
        "stream_options": {"include_usage": True},
        "store": False,
    }
    if request.get("effort") and options.get("includeReasoningEffort", True) is not False:
        params["reasoning_effort"] = request["effort"]
    tools = _convert_chat_completion_tools(request.get("tools", []))
    if tools:
        params["tools"] = tools
    return params


def resolveOpenAiBaseUrl(mode: str, baseUrl: str | None) -> str | None:
    trimmed = baseUrl.strip() if isinstance(baseUrl, str) else ""
    if trimmed:
        return trimmed
    return DEFAULT_CODEX_BASE_URL if mode == "codex" else None


def getOpenAiProviderLabel(mode: str, _baseUrl: str | None = None) -> str:
    if mode == "codex":
        return "OpenAI/Codex"
    if mode == "chat_completions":
        return "OpenAI Chat Completions"
    return "OpenAI Responses"


def isCodexBaseUrl(baseUrl: str | None) -> bool:
    return (baseUrl.strip() if isinstance(baseUrl, str) and baseUrl.strip() else DEFAULT_CODEX_BASE_URL).rstrip("/") == DEFAULT_CODEX_BASE_URL


def supportsOpenAiChatCompletionsReasoningEffort(baseUrl: str | None) -> bool:
    if not baseUrl or not baseUrl.strip():
        return True
    try:
        return urlparse(baseUrl).hostname == "api.openai.com"
    except Exception:
        return False


def convertMessagesToInput(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user":
            if isinstance(content, str):
                out.append({"type": "message", "role": "user", "content": content})
            elif isinstance(content, list):
                user_text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
                if user_text:
                    out.append({"type": "message", "role": "user", "content": user_text})
                for block in content:
                    if block.get("type") == "tool_result":
                        out.append({"type": "function_call_output", "call_id": block.get("tool_use_id"), "output": block.get("content")})
            continue
        assistant_text = content if isinstance(content, str) else "".join(block.get("text", "") for block in content or [] if block.get("type") == "text")
        if assistant_text:
            out.append({"type": "message", "role": "assistant", "content": assistant_text})
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    out.append({"type": "function_call", "call_id": block.get("id"), "name": block.get("name"), "arguments": compact_json(block.get("input") or {})})
    return out


def convertMessagesToChatCompletions(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system.strip():
        out.append({"role": "system", "content": system.strip()})
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                user_text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
                if user_text:
                    out.append({"role": "user", "content": user_text})
                for block in content:
                    if block.get("type") == "tool_result":
                        out.append({"role": "tool", "tool_call_id": block.get("tool_use_id"), "content": block.get("content")})
            continue
        if isinstance(content, str):
            out.append({"role": "assistant", "content": content})
            continue
        assistant_text = "".join(block.get("text", "") for block in content or [] if block.get("type") == "text")
        tool_calls = [
            {"id": block.get("id"), "type": "function", "function": {"name": block.get("name"), "arguments": compact_json(block.get("input") or {})}}
            for block in content or []
            if block.get("type") == "tool_use"
        ]
        if not assistant_text and not tool_calls:
            continue
        item: dict[str, Any] = {"role": "assistant", "content": assistant_text or None}
        reasoning = _extract_provider_reasoning_content(content or [])
        if reasoning and tool_calls:
            item["reasoning_content"] = reasoning
        if tool_calls:
            item["tool_calls"] = tool_calls
        out.append(item)
    return out


def _infer_openai_responses_mode(base_url: str | None) -> str:
    return "codex" if isCodexBaseUrl(base_url) else "responses"


def _create_openai_client(args: dict[str, Any], base_url: str | None) -> Any:
    factory = args.get("clientFactory") or args.get("client_factory")
    if not factory:
        raise RuntimeError("A clientFactory is required unless the optional OpenAI SDK transport is added by the application.")
    options = {"apiKey": args.get("token"), "dangerouslyAllowBrowser": False, "fetch": _build_openai_fetch(args.get("runtime") or {})}
    if args.get("headers"):
        options["defaultHeaders"] = args["headers"]
    if base_url:
        options["baseURL"] = base_url
    return factory(options)


def _build_openai_fetch(runtime: dict[str, Any]):
    async def fetch(_input: Any, init: dict[str, Any] | None = None) -> Any:
        signal = get_value(init or {}, "signal") or runtime.get("abortSignal")
        if _signal_aborted(signal):
            raise RuntimeError("The operation was aborted.")
        raise RuntimeError("OpenAI fetch transport is only available through the configured clientFactory.")

    return fetch


async def _race_openai_activity(
    awaitable: Any,
    *,
    timeout_ms: int,
    provider_label: str,
    phase: str,
    attempt: int,
    had_output: bool,
    had_tool_use: bool,
    abort_signal: Any = None,
) -> Any:
    _raise_openai_cancelled({"abortSignal": abort_signal}, provider_label, phase, attempt, had_output, had_tool_use)
    task = asyncio.ensure_future(awaitable)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (max(1, timeout_ms) / 1000)
    poll_seconds = OPENAI_ABORT_POLL_INTERVAL_MS / 1000
    while True:
        if task.done():
            return task.result()
        if _signal_aborted(abort_signal):
            await _cancel_openai_activity_task(task)
            _raise_openai_cancelled({"abortSignal": abort_signal}, provider_label, phase, attempt, had_output, had_tool_use)
        remaining = deadline - loop.time()
        if remaining <= 0:
            await _cancel_openai_activity_task(task)
            _raise_openai_cancelled({"abortSignal": abort_signal}, provider_label, phase, attempt, had_output, had_tool_use)
            raise _create_openai_timeout(provider_label, phase, attempt, had_output, had_tool_use, timeout_ms)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=min(poll_seconds, remaining))
        except asyncio.TimeoutError:
            if task.done():
                return task.result()
            continue


def _create_openai_timeout(provider_label: str, phase: str, attempt: int, had_output: bool, had_tool_use: bool, timeout_ms: int) -> Any:
    return createProviderRequestError(
        createProviderFailureRecord(
            provider="openai",
            providerLabel=provider_label,
            phase=phase,
            category="timeout",
            retryable=not had_output and not had_tool_use,
            attempt=attempt,
            hadAssistantOutput=had_output,
            hadToolUse=had_tool_use,
            rawMessage=f"OpenAI request timed out after {timeout_ms}ms while waiting for provider activity.",
        )
    )


async def _cancel_openai_activity_task(task: asyncio.Future[Any]) -> None:
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _raise_openai_cancelled(runtime: dict[str, Any], provider_label: str, phase: str, attempt: int, had_output: bool, had_tool_use: bool) -> None:
    if not _signal_aborted(runtime.get("abortSignal")):
        return
    raise createProviderRequestError(
        createProviderFailureRecord(
            provider="openai",
            providerLabel=provider_label,
            phase=phase,
            category="cancelled",
            retryable=False,
            attempt=attempt,
            hadAssistantOutput=had_output,
            hadToolUse=had_tool_use,
            rawMessage="OpenAI request was cancelled.",
        )
    )


def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))


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


def _apply_chat_completion_usage(usage: dict[str, Any], next_usage: Any) -> None:
    if get_value(next_usage, "prompt_tokens") is not None:
        usage["input_tokens"] = get_value(next_usage, "prompt_tokens")
    if get_value(next_usage, "completion_tokens") is not None:
        usage["output_tokens"] = get_value(next_usage, "completion_tokens")


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


def _emit(runtime: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    fn = runtime.get("emitEvent")
    if fn:
        fn(event, data)
