from __future__ import annotations

"""Request-level token estimation over the canonical message encoding."""

import json
from typing import Any

from ..payloads.token_estimation import (
    estimate_single_message_tokens,
    estimate_text_tokens,
    stringify_value_for_token_estimate,
)
from ..providers.openai import build_chat_completions_create_params, build_responses_create_params


def _estimate_request_tokens(request: dict[str, Any], image_input_tokens: int = 0) -> int:
    # role:content per message, like every other estimator (TS parity):
    # serializing whole message dicts with spaced JSON inflated
    # metadata-bearing tool results by 43.5% against the compaction units
    # this number is subtracted from.
    total = estimate_text_tokens(request.get("system", ""))
    for message in request.get("messages", []):
        total += estimate_single_message_tokens(message)
    if request.get("tools"):
        total += estimate_text_tokens(stringify_value_for_token_estimate(request["tools"]))
    return total + image_input_tokens


def _estimate_openai_request_tokens(request: dict[str, Any], mode: str, image_input_tokens: int = 0) -> int:
    if mode == "chat_completions":
        params = build_chat_completions_create_params(request)
        total = 0
        for message in params.get("messages") or []:
            total += estimate_text_tokens(stringify_value_for_token_estimate(message))
        if params.get("tools"):
            total += estimate_text_tokens(stringify_value_for_token_estimate(params["tools"]))
        if params.get("reasoning_effort"):
            total += estimate_text_tokens(json.dumps(params["reasoning_effort"], ensure_ascii=False))
        if params.get("stream_options"):
            total += estimate_text_tokens(json.dumps(params["stream_options"], ensure_ascii=False))
        return total + image_input_tokens
    params = _sanitize_openai_count_params(
        build_responses_create_params({"isCodexBackend": mode == "codex", "request": request})
    )
    total = 0
    if isinstance(params.get("instructions"), str):
        total += estimate_text_tokens(params["instructions"])
    input_value = params.get("input")
    if isinstance(input_value, list):
        for item in input_value:
            total += estimate_text_tokens(stringify_value_for_token_estimate(item))
    elif isinstance(input_value, str):
        total += estimate_text_tokens(input_value)
    if params.get("tools"):
        total += estimate_text_tokens(stringify_value_for_token_estimate(params["tools"]))
    if params.get("reasoning"):
        total += estimate_text_tokens(stringify_value_for_token_estimate(params["reasoning"]))
    return total + image_input_tokens


def _sanitize_openai_count_params(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(value)
    for key in ("stream", "store", "max_output_tokens", "prompt_cache_key", "prompt_cache_retention"):
        sanitized.pop(key, None)
    return sanitized
