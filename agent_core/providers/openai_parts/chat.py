from __future__ import annotations

from .shared import *

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
            params = buildChatCompletionsCreateParams(
                args["request"],
                {
                    "includeReasoningEffort": supportsOpenAiChatCompletionsReasoningEffort(base_url),
                    "includeStore": not base_url or supportsOpenAiChatCompletionsReasoningEffort(base_url),
                    "requestOptions": openAiPromptCacheRequestOptions(
                        mode="chat_completions",
                        base_url=base_url,
                        config=args.get("config") or {},
                        request=args["request"],
                        request_options=args.get("requestOptions") or {},
                    ),
                },
            )
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
