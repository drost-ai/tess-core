from __future__ import annotations

import copy

from .shared import *
from ..prompt_cache_diagnostics import (
    buildPromptCacheRequestSnapshot,
    normalizePromptCacheProviderReason,
)


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

DEFAULT_API_KEY_BETAS = ["fine-grained-tool-streaming-2025-05-14"]
DEFAULT_OAUTH_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "fine-grained-tool-streaming-2025-05-14",
]


def buildAnthropicHeaders(
    config: dict[str, Any],
    options: dict[str, Any] | None = None,
    features: dict[str, Any] | None = None,
) -> dict[str, str]:
    from ...runtime.secret_provider import assertProviderConfigSecretsResolved

    config = assertProviderConfigSecretsResolved(config)
    options = options or {}
    custom_headers = config.get("customHeaders") or {}
    unrelated_custom_headers = {
        name: value
        for name, value in custom_headers.items()
        if str(name).lower() not in {"anthropic-beta", "authorization", "x-api-key"}
    }
    headers = {
        "accept": "text/event-stream" if options.get("stream") else "application/json",
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": buildAnthropicBetaHeader(config, features or {}),
        "anthropic-dangerous-direct-browser-access": "true",
        "user-agent": (
            "claude-cli/2.1.2 (external, cli)"
            if _is_kimi_anthropic_coding_backend(config)
            else "tess/0.1.0 (sdk-cli)"
        ),
        "x-app": "cli",
        **unrelated_custom_headers,
    }
    if config.get("authToken"):
        headers["authorization"] = f"Bearer {config['authToken']}"
    elif config.get("apiKey"):
        headers["x-api-key"] = config["apiKey"]
    else:
        raise RuntimeError("No Anthropic credential is configured.")
    return headers


def buildAnthropicBetaHeader(
    config: dict[str, Any], features: dict[str, Any] | None = None
) -> str:
    from ...runtime.secret_provider import assertProviderConfigSecretsResolved

    config = assertProviderConfigSecretsResolved(config)
    features = features or {}
    values = list(DEFAULT_OAUTH_BETAS if config.get("authToken") else DEFAULT_API_KEY_BETAS)
    if not config.get("authToken") and _is_kimi_anthropic_coding_backend(config):
        values.insert(0, "claude-code-20250219")
    if features.get("providerDiagnostics"):
        values.append("cache-diagnosis-2026-04-07")
    custom_headers = config.get("customHeaders") or {}
    for name, value in sorted(custom_headers.items(), key=lambda item: str(item[0])):
        if str(name).lower() == "anthropic-beta":
            values.extend(str(value).split(","))
    seen: set[str] = set()
    merged: list[str] = []
    for token in values:
        normalized = str(token).strip()
        logical = normalized.lower()
        if not normalized or logical in seen:
            continue
        seen.add(logical)
        merged.append(normalized)
    return ",".join(merged)

def buildAnthropicCountTokensPayload(request: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = buildAnthropicMessagesPayload(request, {"stream": False, **(options or {})})
    payload.pop("max_tokens", None)
    payload.pop("stream", None)
    payload.pop("output_config", None)
    return sanitizeAnthropicCountPayload(payload)

def buildAnthropicMessagesPayload(request: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    built = _build_anthropic_payload_parts(request, options)
    if _anthropic_provider_diagnostics_enabled(built["policy"]):
        built["payload"]["diagnostics"] = {"previous_message_id": None}
    return built["payload"]


def buildAnthropicMessagesEnvelope(
    request: dict[str, Any],
    config: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    built = _build_anthropic_payload_parts(request, {**(options or {}), "config": config})
    policy = built["policy"]
    diagnostics_requested = _anthropic_provider_diagnostics_enabled(policy)
    header_features = {"providerDiagnostics": diagnostics_requested}
    dispatch = policy.get("dispatchContext") if isinstance(policy.get("dispatchContext"), dict) else None
    envelope = {
        "payload": built["payload"],
        "cachePlan": built["plan"],
        "headerFeatures": header_features,
        "diagnostics": {"requested": diagnostics_requested, "previousMessageIdSent": False},
    }
    if not dispatch or not policy.get("enabled"):
        if diagnostics_requested:
            built["payload"]["diagnostics"] = {"previous_message_id": None}
        return envelope
    snapshot = buildPromptCacheRequestSnapshot(
        {
            "providerProfileName": config.get("providerProfileName") or config.get("provider") or "anthropic",
            "routeClass": _prompt_cache_route_class(policy.get("compatibility")),
            "baseUrl": config.get("anthropicBaseUrl"),
            "model": request.get("model"),
            "betaTokens": [token.strip() for token in buildAnthropicBetaHeader(config, header_features).split(",") if token.strip()],
            "normalizedTools": built["normalizedTools"],
            "normalizedSystem": built["normalizedSystem"],
            "normalizedMessages": built["normalizedMessages"],
            "toolChoice": request.get("toolChoice") or request.get("tool_choice"),
            "outputConfig": built.get("outputConfig"),
            "policy": policy,
            "plan": built["plan"],
            "preparation": dispatch.get("preparation"),
            "reactiveAttempt": (dispatch.get("dispatchOrder") or {}).get("reactiveAttempt"),
        }
    )
    previous_message_id = (
        _eligible_previous_diagnostic_id(dispatch.get("previous"), snapshot)
        if diagnostics_requested
        else None
    )
    if diagnostics_requested:
        built["payload"]["diagnostics"] = {"previous_message_id": previous_message_id}
    envelope["diagnostics"]["previousMessageIdSent"] = previous_message_id is not None
    envelope["promptCache"] = {
        "sessionId": dispatch.get("sessionId"),
        "paths": dispatch.get("paths"),
        "dispatchOrder": copy.deepcopy(dispatch.get("dispatchOrder")),
        "plan": copy.deepcopy(built["plan"]),
        "snapshot": snapshot,
        "ttl": policy.get("ttl"),
        "scope": policy.get("scope"),
    }
    return envelope


def _build_anthropic_payload_parts(
    request: dict[str, Any], options: dict[str, Any] | None = None
) -> dict[str, Any]:
    options = options or {}
    config = options.get("config") if isinstance(options.get("config"), dict) else {}
    policy = resolvePromptCachePolicy(config=config, request=request, options=options)
    system_blocks = [{
        "type": "text",
        "text": "x-anthropic-billing-header: cc_version=0.1.0; cc_entrypoint=tess; cch=00000;",
        "_cache_target": False,
    }]
    system_text = str(request.get("system") or "").strip()
    if system_text:
        system_blocks.append({"type": "text", "text": system_text})
    normalized_messages = [
        {"role": message.get("role"), "content": normalizeAnthropicContentBlocks(message.get("content"))}
        for message in request.get("messages", [])
    ]
    normalized_tools = [
        _normalize_anthropic_tool_definition(tool) for tool in request.get("tools") or []
    ]
    prior = request.get("promptCache") or request.get("prompt_cache") or {}
    plan = planAnthropicCache(
        {
            "policy": policy,
            "normalizedSystem": system_blocks,
            "normalizedTools": normalized_tools,
            "normalizedMessages": normalized_messages,
            "previousAnchor": prior.get("messageTailPreviousAnchor")
            if isinstance(prior, dict)
            else None,
        }
    )
    marked = applyAnthropicCachePlan(
        {"system": system_blocks, "tools": normalized_tools, "messages": normalized_messages},
        plan,
    )
    payload: dict[str, Any] = {
        "model": request["model"],
        "max_tokens": _resolve_max_output_tokens(request),
        "system": marked["system"],
        "messages": marked["messages"],
    }
    if marked["tools"]:
        payload["tools"] = marked["tools"]
    if plan["automatic"]:
        payload["cache_control"] = copy.deepcopy(plan["cacheControl"])
    if options.get("stream"):
        payload["stream"] = True
    effort = request.get("effort")
    if effort:
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": effort}
    if countAnthropicCacheControls(payload) != plan["usedBreakpoints"]:
        raise RuntimeError("Anthropic cache plan does not match serialized controls.")
    return {
        "payload": payload,
        "plan": plan,
        "policy": policy,
        "normalizedSystem": system_blocks,
        "normalizedTools": normalized_tools,
        "normalizedMessages": normalized_messages,
        "outputConfig": payload.get("output_config"),
    }


def _anthropic_provider_diagnostics_enabled(policy: dict[str, Any]) -> bool:
    return bool(
        policy.get("enabled")
        and policy.get("providerDiagnostics")
        and policy.get("compatibility") == "official"
        and not policy.get("compatibilitySuppressedReason")
    )


def _eligible_previous_diagnostic_id(previous: Any, current: dict[str, Any]) -> str | None:
    if not isinstance(previous, dict) or not isinstance(previous.get("responseId"), str):
        return None
    prior = previous.get("request")
    if not isinstance(prior, dict):
        return None
    fields = (
        "providerProfileNameHash",
        "routeClass",
        "endpointIdentityHash",
        "modelHash",
        "betasHash",
        "cachePolicyHash",
    )
    return previous["responseId"] if all(prior.get(key) == current.get(key) for key in fields) else None


def _prompt_cache_route_class(compatibility: Any) -> str:
    return compatibility if compatibility in {"official", "claude_code", "compatible", "bedrock"} else "official"


def planAnthropicCacheBreakpoints(
    request: dict[str, Any], options: dict[str, Any] | None = None
) -> dict[str, Any]:
    options = options or {}
    config = options.get("config") if isinstance(options.get("config"), dict) else {}
    policy = resolvePromptCachePolicy(config=config, request=request, options=options)
    system = [{
        "type": "text",
        "text": "x-anthropic-billing-header: cc_version=0.1.0; cc_entrypoint=tess; cch=00000;",
        "_cache_target": False,
    }]
    if str(request.get("system") or "").strip():
        system.append({"type": "text", "text": str(request["system"]).strip()})
    messages = [
        {"role": item.get("role"), "content": normalizeAnthropicContentBlocks(item.get("content"))}
        for item in request.get("messages") or []
    ]
    tools = [_normalize_anthropic_tool_definition(item) for item in request.get("tools") or []]
    prior = request.get("promptCache") or request.get("prompt_cache") or {}
    return planAnthropicCache(
        {
            "policy": policy,
            "normalizedSystem": system,
            "normalizedTools": tools,
            "normalizedMessages": messages,
            "previousAnchor": prior.get("messageTailPreviousAnchor")
            if isinstance(prior, dict)
            else None,
        }
    )


def anthropicCacheBreakpointMessageIndices(plan: dict[str, Any]) -> list[int]:
    return [
        int(anchor["messageIndex"])
        for anchor in (plan.get("tailA"), plan.get("tailB"))
        if isinstance(anchor, dict) and isinstance(anchor.get("messageIndex"), int)
    ]

def _mark_last_text_block_for_cache(blocks: list[dict[str, Any]], cache_control: dict[str, str]) -> None:
    for index in range(len(blocks) - 1, -1, -1):
        block = blocks[index]
        if block.get("type") == "text":
            blocks[index] = {**block, "cache_control": dict(cache_control)}
            return
    blocks.append({"type": "text", "text": "", "cache_control": dict(cache_control)})

def _resolve_max_output_tokens(request: dict[str, Any]) -> int:
    for key in ("maxOutputTokens", "max_output_tokens", "maxTokens", "max_tokens"):
        value = request.get(key)
        try:
            parsed = int(float(str(value).strip())) if str(value).strip() else 0
        except Exception:
            continue
        if parsed > 0:
            return parsed
    return 16384

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
                source = block.get("source") if isinstance(block.get("source"), dict) else block
                data = str(source.get("data") or source.get("data_base64") or source.get("base64") or "").strip()
                mime_type = str(source.get("media_type") or source.get("mime_type") or source.get("mimeType") or source.get("mime") or "image/jpeg").strip() or "image/jpeg"
                if data and _valid_base64_image_payload(data):
                    out.append({"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": data}})
            elif block.get("type") == "tool_use":
                out.append({"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}})
            elif block.get("type") == "tool_result":
                tool_result = {"type": "tool_result", "tool_use_id": block.get("tool_use_id"), "content": str(block.get("content", ""))}
                if isinstance(block.get("is_error"), bool):
                    tool_result["is_error"] = block["is_error"]
                out.append(tool_result)
        return out
    return []

def _valid_base64_image_payload(value: str) -> bool:
    raw = re.sub(r"\s+", "", str(value or "").strip())
    if not raw:
        return False
    try:
        base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True

_build_anthropic_beta_header = buildAnthropicBetaHeader
