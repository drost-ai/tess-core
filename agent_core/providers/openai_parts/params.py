from __future__ import annotations

from .shared import *

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
    return _merge_request_options(params, args.get("requestOptions"), _RESPONSES_TOP_LEVEL_REQUEST_OPTIONS)

def buildChatCompletionsCreateParams(request: dict[str, Any] | None = None, options: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    request = {**(request or {}), **kwargs}
    options = options or {}
    params: dict[str, Any] = {
        "model": request["model"],
        "messages": convertMessagesToChatCompletions(request.get("system", ""), request.get("messages", [])),
        "max_tokens": 16384,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if options.get("includeStore", True) is not False:
        params["store"] = False
    if request.get("effort") and options.get("includeReasoningEffort", True) is not False:
        params["reasoning_effort"] = request["effort"]
    tools = _convert_chat_completion_tools(request.get("tools", []))
    if tools:
        params["tools"] = tools
    return _merge_request_options(params, options.get("requestOptions"), _CHAT_TOP_LEVEL_REQUEST_OPTIONS)


_CHAT_TOP_LEVEL_REQUEST_OPTIONS = {
    "audio",
    "extra_headers",
    "extra_query",
    "frequency_penalty",
    "function_call",
    "functions",
    "logit_bias",
    "logprobs",
    "max_completion_tokens",
    "max_tokens",
    "metadata",
    "modalities",
    "n",
    "parallel_tool_calls",
    "prediction",
    "presence_penalty",
    "prompt_cache_retention",
    "reasoning_effort",
    "response_format",
    "seed",
    "service_tier",
    "stop",
    "store",
    "temperature",
    "timeout",
    "tool_choice",
    "top_logprobs",
    "top_p",
    "user",
}

_RESPONSES_TOP_LEVEL_REQUEST_OPTIONS = {
    "background",
    "extra_headers",
    "extra_query",
    "include",
    "max_output_tokens",
    "metadata",
    "parallel_tool_calls",
    "previous_response_id",
    "prompt",
    "prompt_cache_key",
    "prompt_cache_retention",
    "reasoning",
    "service_tier",
    "temperature",
    "text",
    "timeout",
    "tool_choice",
    "top_p",
    "truncation",
    "user",
}


def _merge_request_options(
    params: dict[str, Any],
    request_options: Any,
    top_level_options: set[str],
) -> dict[str, Any]:
    if not isinstance(request_options, dict) or not request_options:
        return params
    protected = {"model", "messages", "input", "instructions", "tools", "stream", "stream_options"}
    merged = dict(params)
    extra_body = dict(merged.get("extra_body") or {}) if isinstance(merged.get("extra_body"), dict) else {}
    for key, value in request_options.items():
        cleaned = str(key or "").strip()
        if not cleaned or cleaned in protected:
            continue
        if cleaned == "extra_body":
            if isinstance(value, dict):
                extra_body.update(value)
            continue
        if cleaned in top_level_options:
            merged[cleaned] = value
            continue
        extra_body[cleaned] = value
    if extra_body:
        merged["extra_body"] = extra_body
    return merged

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
                user_content, function_outputs = _convert_responses_user_content(content)
                if user_content:
                    out.append(
                        {
                            "type": "message",
                            "role": "user",
                            "content": _collapse_responses_text_content(user_content),
                        }
                    )
                out.extend(function_outputs)
            continue
        assistant_text = content if isinstance(content, str) else "".join(block.get("text", "") for block in content or [] if block.get("type") == "text")
        if assistant_text:
            out.append({"type": "message", "role": "assistant", "content": assistant_text})
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    out.append({"type": "function_call", "call_id": block.get("id"), "name": block.get("name"), "arguments": compact_json(block.get("input") or {})})
    return out

def _convert_responses_user_content(content: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    user_content: list[dict[str, Any]] = []
    function_outputs: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            user_content.append({"type": "input_text", "text": str(block)})
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type in ("text", "input_text"):
            user_content.append({"type": "input_text", "text": str(block.get("text") or "")})
            continue
        if block_type in ("image", "input_image"):
            image_url = _image_data_url(block)
            if image_url:
                user_content.append({"type": "input_image", "detail": str(block.get("detail") or "auto"), "image_url": image_url})
            continue
        if block_type == "tool_result":
            function_outputs.append({"type": "function_call_output", "call_id": block.get("tool_use_id"), "output": block.get("content")})
            continue
        user_content.append({"type": "input_text", "text": str(block)})
    return user_content, function_outputs

def _collapse_responses_text_content(content: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    if any(block.get("type") == "input_image" for block in content):
        return content
    return "".join(str(block.get("text") or "") for block in content if block.get("type") == "input_text")

def _image_data_url(block: dict[str, Any]) -> str:
    existing = block.get("image_url") or block.get("url")
    if isinstance(existing, dict):
        existing = existing.get("url")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    data = block.get("data") or block.get("data_base64") or block.get("base64")
    raw = str(data or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("data:"):
        return raw
    mime_type = str(block.get("mime_type") or block.get("mimeType") or block.get("mime") or "image/jpeg").strip() or "image/jpeg"
    return f"data:{mime_type};base64,{raw}"

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
                user_content, tool_messages = _convert_chat_user_content(content)
                if user_content:
                    out.append({"role": "user", "content": _collapse_chat_text_content(user_content)})
                out.extend(tool_messages)
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

def _convert_chat_user_content(content: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    user_content: list[dict[str, Any]] = []
    tool_messages: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            user_content.append({"type": "text", "text": str(block)})
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type in ("text", "input_text"):
            user_content.append({"type": "text", "text": str(block.get("text") or "")})
            continue
        if block_type in ("image", "input_image"):
            image_url = _image_data_url(block)
            if image_url:
                user_content.append({"type": "image_url", "image_url": {"url": image_url, "detail": str(block.get("detail") or "auto")}})
            continue
        if block_type == "tool_result":
            tool_messages.append({"role": "tool", "tool_call_id": block.get("tool_use_id"), "content": block.get("content")})
            continue
        user_content.append({"type": "text", "text": str(block)})
    return user_content, tool_messages

def _collapse_chat_text_content(content: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    if any(block.get("type") == "image_url" for block in content):
        return content
    return "".join(str(block.get("text") or "") for block in content if block.get("type") == "text")
