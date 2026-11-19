"""Build an Interactions request from the runtime's provider request.

Every request is stateless: ``store`` is false and the whole conversation is sent as
steps, so nothing about a session is kept by Google and a session can be compacted,
forked, resumed or moved to another provider from its own record.

The steps of a conversation are rebuilt the same way on every call, and each call's
steps begin with the previous call's. That stable prefix is what Gemini's implicit
cache reads, and it is the only caching the Interactions API offers.
"""
from __future__ import annotations

import base64
import binascii
import copy
import re
from typing import Any

from ..token_budget import resolve_request_max_output_tokens
from .thinking import google_thinking_levels, resolve_google_thinking_level

DEFAULT_GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GOOGLE_PROVIDER_LABEL = "Google Gemini"

# A thought step is Gemini's reasoning between two outputs. It must go back exactly as
# it came, so each one is kept on the block it preceded, under provider_metadata, where
# the other providers' request builders never look.
THOUGHTS_BEFORE = "google_thoughts"
THOUGHTS_AFTER = "google_trailing_thoughts"

# Request options a profile may set. ``store``, ``stream``, ``input``, ``tools`` and
# ``previous_interaction_id`` are the runtime's to decide and cannot be overridden.
_TOP_LEVEL_REQUEST_OPTIONS = frozenset({"service_tier", "response_format"})
_RESERVED_GENERATION_CONFIG = frozenset({"max_output_tokens"})

# JSON Schema annotations that describe a document rather than a value. Gemini reads
# a subset of JSON Schema and has no use for these.
_SCHEMA_ANNOTATIONS = frozenset({"$schema", "$id", "$comment"})


def resolve_google_base_url(base_url: str | None) -> str:
    return str(base_url or "").strip().rstrip("/") or DEFAULT_GOOGLE_BASE_URL


def build_interactions_create_params(request: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    """The JSON body of ``POST /interactions`` for one provider call."""
    options = options or {}
    model = str(request["model"])
    params: dict[str, Any] = {"model": model}
    system = str(request.get("system") or "").strip()
    if system:
        params["system_instruction"] = system
    tools = convert_tools(request.get("tools") or [])
    if tools:
        params["tools"] = tools
    params["input"] = convert_messages_to_steps(request.get("messages") or [])
    generation_config: dict[str, Any] = {}
    level = resolve_google_thinking_level(model, request.get("effort"))
    if level:
        generation_config["thinking_level"] = level
    if google_thinking_levels(model):
        generation_config["thinking_summaries"] = "auto"
    request_options = options.get("requestOptions") if isinstance(options.get("requestOptions"), dict) else {}
    extra_config = request_options.get("generation_config")
    if isinstance(extra_config, dict):
        generation_config.update(
            {str(key): copy.deepcopy(value) for key, value in extra_config.items() if key not in _RESERVED_GENERATION_CONFIG}
        )
    generation_config["max_output_tokens"] = resolve_request_max_output_tokens(request)
    params["generation_config"] = generation_config
    for key in sorted(_TOP_LEVEL_REQUEST_OPTIONS):
        if key in request_options:
            params[key] = copy.deepcopy(request_options[key])
    params["stream"] = True
    params["store"] = False
    return params


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": str(tool.get("description") or ""),
            "parameters": sanitize_google_schema(tool.get("input_schema") or {}),
        }
        for tool in tools
    ]


def sanitize_google_schema(schema: Any) -> dict[str, Any]:
    """A tool's input schema as Gemini takes it: an object schema, without document annotations."""
    cleaned = _clean_schema(schema)
    if not isinstance(cleaned, dict):
        cleaned = {}
    if cleaned.get("type") != "object":
        cleaned = {**cleaned, "type": "object"}
    if not isinstance(cleaned.get("properties"), dict):
        cleaned["properties"] = {}
    return cleaned


def _clean_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_clean_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for key, child in value.items():
        if key in _SCHEMA_ANNOTATIONS:
            continue
        if key == "properties" and isinstance(child, dict):
            # Property names are data, not keywords: a property called "$id" stays.
            cleaned[key] = {str(name): _clean_schema(item) for name, item in child.items()}
        else:
            cleaned[str(key)] = _clean_schema(child)
    return cleaned


def convert_messages_to_steps(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation as Interactions steps, in order."""
    steps: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user":
            steps.extend(_user_steps(content, tool_names))
        else:
            steps.extend(_assistant_steps(content, tool_names))
    return steps


def _user_steps(content: Any, tool_names: dict[str, str]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [_user_input([_text(content)])] if content else []
    if not isinstance(content, list):
        return []
    results: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(_text(str(block)))
            continue
        kind = str(block.get("type") or "").strip()
        if kind in ("text", "input_text"):
            if block.get("text"):
                parts.append(_text(str(block["text"])))
        elif kind in ("image", "input_image"):
            image = _image(block)
            if image:
                images.append(image)
        elif kind == "tool_result":
            results.append(_function_result(block, tool_names))
        else:
            parts.append(_text(str(block)))
    if results and images:
        # An image beside tool results is one a tool returned. It goes back inside the
        # last result, where Gemini reads it as part of the tool's answer.
        last = results[-1]["result"]
        results[-1]["result"] = [*(last if isinstance(last, list) else []), *images]
        images = []
    steps = list(results)
    if parts or images:
        steps.append(_user_input([*parts, *images]))
    return steps


def _function_result(block: dict[str, Any], tool_names: dict[str, str]) -> dict[str, Any]:
    call_id = str(block.get("tool_use_id") or "")
    step: dict[str, Any] = {"type": "function_result"}
    if tool_names.get(call_id):
        step["name"] = tool_names[call_id]
    step["call_id"] = call_id
    raw = block.get("content")
    result: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("type") in ("image", "input_image"):
                image = _image(item)
                if image:
                    result.append(image)
            elif isinstance(item, dict) and item.get("type") in ("text", "input_text"):
                result.append(_text(str(item.get("text") or "")))
            else:
                result.append(_text(str(item)))
    else:
        result.append(_text("" if raw is None else str(raw)))
    # Gemini refuses a text item with no text. A tool that printed nothing answers
    # with the empty string, which it accepts.
    result = [item for item in result if item.get("type") != "text" or item.get("text")]
    step["result"] = result or ""
    if block.get("is_error") is True:
        step["is_error"] = True
    return step


def _assistant_steps(content: Any, tool_names: dict[str, str]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [_model_output(content)] if content else []
    if not isinstance(content, list):
        return []
    steps: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind not in ("text", "tool_use"):
            # Another provider's reasoning blocks mean nothing to Gemini.
            continue
        metadata = block.get("provider_metadata") if isinstance(block.get("provider_metadata"), dict) else {}
        steps.extend(_thoughts(metadata.get(THOUGHTS_BEFORE)))
        if kind == "text":
            if block.get("text"):
                steps.append(_model_output(str(block["text"])))
        else:
            call_id, name = str(block.get("id") or ""), str(block.get("name") or "")
            tool_names[call_id] = name
            arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
            steps.append({"type": "function_call", "id": call_id, "name": name, "arguments": copy.deepcopy(arguments)})
        steps.extend(_thoughts(metadata.get(THOUGHTS_AFTER)))
    return steps


def _thoughts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(step) for step in value if isinstance(step, dict) and step.get("type") == "thought"]


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _user_input(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "user_input", "content": content}


def _model_output(text: str) -> dict[str, Any]:
    return {"type": "model_output", "content": [_text(text)]}


def _image(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("source") if isinstance(block.get("source"), dict) else block
    data = re.sub(r"\s+", "", str(source.get("data") or source.get("data_base64") or source.get("base64") or ""))
    if data.lower().startswith("data:"):
        header, _, data = data.partition(";base64,")
        mime_type = header[5:]
    else:
        mime_type = str(source.get("media_type") or source.get("mime_type") or source.get("mimeType") or source.get("mime") or "")
    if not data:
        return None
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    return {"type": "image", "mime_type": mime_type.strip() or "image/jpeg", "data": data}
