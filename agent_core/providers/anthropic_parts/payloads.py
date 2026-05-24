from __future__ import annotations

from .shared import *


def _normalize_anthropic_api_base_url(baseUrl: str | None) -> str | None:
    base = baseUrl.rstrip("/") if isinstance(baseUrl, str) and baseUrl.strip() else ""
    if not base:
        return None
    if base.endswith("/messages"):
        return base[: -len("/messages")]
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _is_kimi_anthropic_coding_backend(config: dict[str, Any]) -> bool:
    return isKimiAnthropicCodingBackend(config)


def resolveAnthropicMessagesUrl(baseUrl: str | None) -> str:
    base = _normalize_anthropic_api_base_url(baseUrl)
    if not base:
        return MESSAGES_API_URL
    return f"{base}/messages?beta=true"

def resolveAnthropicCountTokensUrl(baseUrl: str | None) -> str:
    base = _normalize_anthropic_api_base_url(baseUrl)
    if not base:
        return MESSAGES_COUNT_TOKENS_API_URL
    return f"{base}/messages/count_tokens"

def buildAnthropicHeaders(config: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, str]:
    options = options or {}
    headers = {
        "accept": "text/event-stream" if options.get("stream") else "application/json",
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": _build_anthropic_beta_header(config),
        "anthropic-dangerous-direct-browser-access": "true",
        "user-agent": (
            "claude-cli/2.1.2 (external, cli)"
            if _is_kimi_anthropic_coding_backend(config)
            else "tess/0.1.0 (sdk-cli)"
        ),
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

def buildAnthropicCountTokensPayload(request: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = buildAnthropicMessagesPayload(request, {"stream": False, **(options or {})})
    payload.pop("max_tokens", None)
    payload.pop("stream", None)
    payload.pop("output_config", None)
    return payload

def buildAnthropicMessagesPayload(request: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    config = options.get("config") if isinstance(options.get("config"), dict) else {}
    system_blocks = [
        {"type": "text", "text": "x-anthropic-billing-header: cc_version=0.1.0; cc_entrypoint=tess; cch=00000;"},
    ]
    system_text = str(request.get("system") or "").strip()
    if system_text:
        system_blocks.append({"type": "text", "text": system_text})
    if anthropicExplicitCacheEnabled(config, request, options):
        _mark_last_text_block_for_cache(system_blocks, anthropicCacheControl(config, request, options))
    payload: dict[str, Any] = {
        "model": request["model"],
        "max_tokens": 16384,
        "system": system_blocks,
        "messages": [{"role": message.get("role"), "content": normalizeAnthropicContentBlocks(message.get("content"))} for message in request.get("messages", [])],
    }
    if anthropicAutomaticCacheEnabled(config, request, options):
        payload["cache_control"] = anthropicCacheControl(config, request, options)
    if options.get("stream"):
        payload["stream"] = True
    effort = request.get("effort")
    if effort:
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": effort}
    tools = request.get("tools") or []
    if tools:
        payload["tools"] = [_normalize_anthropic_tool_definition(tool) for tool in tools]
    return payload

def _mark_last_text_block_for_cache(blocks: list[dict[str, Any]], cache_control: dict[str, str]) -> None:
    for index in range(len(blocks) - 1, -1, -1):
        block = blocks[index]
        if block.get("type") == "text":
            blocks[index] = {**block, "cache_control": dict(cache_control)}
            return
    blocks.append({"type": "text", "text": "", "cache_control": dict(cache_control)})

def _normalize_anthropic_tool_definition(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {"name": "", "description": "", "input_schema": {"type": "object", "properties": {}}}
    return {
        "name": tool.get("name"),
        "description": tool.get("description"),
        "input_schema": _normalize_anthropic_tool_schema(tool.get("input_schema") or {}),
    }

def _normalize_anthropic_tool_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_normalize_anthropic_tool_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, list):
            non_null_types = [item for item in value if item != "null"]
            if len(non_null_types) == 1:
                out[key] = non_null_types[0]
            elif non_null_types:
                out[key] = non_null_types
            continue
        if key == "enum" and isinstance(value, list):
            enum_values = [item for item in value if item is not None]
            if enum_values:
                out[key] = [_normalize_anthropic_tool_schema(item) for item in enum_values]
            continue
        out[key] = _normalize_anthropic_tool_schema(value)
    return out

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
            elif block.get("type") == "image":
                data = str(block.get("data") or block.get("data_base64") or block.get("base64") or "").strip()
                mime_type = str(block.get("mime_type") or block.get("mimeType") or block.get("mime") or "image/jpeg").strip() or "image/jpeg"
                if data:
                    out.append({"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": data}})
            elif block.get("type") == "tool_use":
                out.append({"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}})
            elif block.get("type") == "tool_result":
                out.append({"type": "tool_result", "tool_use_id": block.get("tool_use_id"), "content": str(block.get("content", "")), "is_error": block.get("is_error")})
        return out
    return []

def _build_anthropic_beta_header(config: dict[str, Any]) -> str:
    values = ["fine-grained-tool-streaming-2025-05-14"]
    if config.get("authToken"):
        values = ["claude-code-20250219", "oauth-2025-04-20", *values]
    elif _is_kimi_anthropic_coding_backend(config):
        values = ["claude-code-20250219", *values]
    return ",".join(dict.fromkeys(values))
