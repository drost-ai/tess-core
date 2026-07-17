from __future__ import annotations

from ...contracts.providers import ProviderResponse

from .shared import *

async def sendOpenAiRequest(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderResponse:
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
        reasoning_text = ""
        emitted_tool_ids: set[str] = set()
        pending_by_item_id: dict[str, dict[str, Any]] = {}
        item_to_call_id: dict[str, str] = {}
        item_to_name: dict[str, str] = {}
        open_output_items: dict[str, str] = {}
        completed_output_items: set[str] = set()
        tool_argument_bytes_by_item: dict[str, int] = {}
        saw_completed = False
        runtime = args.get("runtime") or {}
        request_start_timeout_ms = int(args.get("requestStartTimeoutMs") or OPENAI_REQUEST_START_TIMEOUT_MS)
        stream_idle_timeout_ms = int(args.get("streamIdleTimeoutMs") or OPENAI_STREAM_IDLE_TIMEOUT_MS)

        def incomplete_response(reason: str) -> Any:
            return createProviderIncompleteResponseError(
                provider="openai",
                providerLabel=provider_label,
                mode=mode,
                phase=phase,
                expectedTerminal="response.completed after every output item completed",
                reason=reason,
                attempt=attempt,
                hadAssistantOutput=had_output,
                hadToolUse=had_tool_use,
                partialAssistantBytes=len(f"{reasoning_text}{emitted_text}".encode("utf-8")),
                partialToolArgumentBytes=sum(tool_argument_bytes_by_item.values()),
                openItems=len(open_output_items) + len(pending_by_item_id),
            )

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
                if saw_completed:
                    raise incomplete_response(
                        f"event {typ or '<unknown>'} arrived after response.completed"
                    )
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
                if typ in {
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                }:
                    delta = str(get_value(event, "delta", "") or "")
                    if not delta:
                        continue
                    reasoning_text += delta
                    if runtime.get("includePartialMessages"):
                        _emit(
                            runtime,
                            "message.assistant.thinking.partial",
                            {
                                "session_id": None,
                                "model": args["request"]["model"],
                                "thinking_delta": delta,
                                "thinking": reasoning_text,
                            },
                        )
                    continue
                if typ == "response.output_item.added":
                    item = get_value(event, "item", {})
                    item_id = str(get_value(item, "id", "") or "")
                    if not item_id:
                        raise incomplete_response("an output item started without an id")
                    if item_id in open_output_items or item_id in completed_output_items:
                        raise incomplete_response(f"output item {item_id} started more than once")
                    open_output_items[item_id] = str(get_value(item, "type", "unknown") or "unknown")
                    if get_value(item, "type") == "function_call":
                        call_id = str(get_value(item, "call_id", "") or "")
                        if call_id:
                            item_to_call_id[item_id] = call_id
                        name = str(get_value(item, "name", "") or "")
                        if name:
                            item_to_name[item_id] = name
                    continue
                if typ == "response.function_call_arguments.done":
                    item_id = str(get_value(event, "item_id", "") or "")
                    name = str(get_value(event, "name", "") or "") or item_to_name.get(item_id, "")
                    raw = str(get_value(event, "arguments", "") or "")
                    if not item_id or not name:
                        raise incomplete_response(
                            "function-call arguments completed without an item id and name"
                        )
                    if item_id not in open_output_items:
                        raise incomplete_response(
                            f"function-call arguments completed before output item {item_id} started"
                        )
                    if item_id in pending_by_item_id:
                        raise incomplete_response(
                            f"function-call arguments for output item {item_id} completed more than once"
                        )
                    try:
                        input_value = _parse_complete_json_object(raw)
                    except Exception as exc:
                        tool_argument_bytes_by_item[item_id] = len(raw.encode("utf-8"))
                        raise incomplete_response(str(exc) or "tool arguments were malformed") from exc
                    tool_argument_bytes_by_item[item_id] = len(raw.encode("utf-8"))
                    pending_by_item_id[item_id] = {
                        "name": name,
                        "raw": raw,
                        "input": input_value,
                    }
                    continue
                if typ == "response.output_item.done":
                    item = get_value(event, "item", {})
                    item_id = str(get_value(item, "id", None) or get_value(event, "item_id", "") or "")
                    if not item_id or item_id not in open_output_items:
                        raise incomplete_response(
                            "an output item completed without a matching start event"
                        )
                    if get_value(item, "type") == "function_call":
                        pending = pending_by_item_id.get(item_id)
                        call_id = str(
                            get_value(item, "call_id", "")
                            or item_to_call_id.get(item_id, "")
                            or ""
                        )
                        name = str(get_value(item, "name", "") or (pending or {}).get("name") or "")
                        raw_value = get_value(item, "arguments")
                        raw = raw_value if isinstance(raw_value, str) else str((pending or {}).get("raw") or "")
                        if not call_id or not name:
                            raise incomplete_response(
                                f"function-call output item {item_id} completed without a call id and name"
                            )
                        if call_id in emitted_tool_ids:
                            raise incomplete_response(f"function call {call_id} completed more than once")
                        input_value = (pending or {}).get("input")
                        if not isinstance(input_value, dict) or raw != (pending or {}).get("raw"):
                            try:
                                input_value = _parse_complete_json_object(raw)
                            except Exception as exc:
                                tool_argument_bytes_by_item[item_id] = len(raw.encode("utf-8"))
                                raise incomplete_response(str(exc) or "tool arguments were malformed") from exc
                        tool_argument_bytes_by_item[item_id] = len(raw.encode("utf-8"))
                        emitted_tool_ids.add(call_id)
                        had_tool_use = True
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": name,
                                "input": input_value,
                            }
                        )
                    del open_output_items[item_id]
                    completed_output_items.add(item_id)
                    pending_by_item_id.pop(item_id, None)
                    continue
                if typ == "response.completed":
                    phase = "response_complete"
                    if open_output_items or pending_by_item_id:
                        raise incomplete_response(
                            "response.completed arrived while output items were still open"
                        )
                    completed = get_value(event, "response", {}) or {}
                    output = get_value(completed, "output", [])
                    output = output if isinstance(output, list) else []
                    try:
                        final_blocks = _extract_output_blocks(output)
                    except Exception as exc:
                        raise incomplete_response(str(exc) or "final output was malformed") from exc
                    for block in final_blocks:
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
                    saw_completed = True
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
                if typ == "response.cancelled":
                    phase = "response_complete"
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
                            rawMessage=_response_error_text(get_value(event, "response")) or "OpenAI response was cancelled.",
                            streamEventType=typ,
                        )
                    )
                if typ == "response.incomplete":
                    phase = "response_complete"
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
            if not saw_completed:
                raise incomplete_response(
                    "the transport reached EOF before response.completed"
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
