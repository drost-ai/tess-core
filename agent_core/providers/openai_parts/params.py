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
    }
    if options.get("includeStore", True) is not False:
        params["store"] = False
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
