from __future__ import annotations

from .shared import *

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
            params = buildResponsesCreateParams(
                {
                    "isCodexBackend": mode == "codex",
                    "request": args["request"],
                    "requestOptions": openAiPromptCacheRequestOptions(
                        mode=mode,
                        base_url=base_url,
                        config=args.get("config") or {},
                        request=args["request"],
                        request_options=args.get("requestOptions") or {},
                    ),
                }
            )
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
