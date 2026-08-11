from __future__ import annotations

"""Request-level token estimation over the canonical message encoding."""

import json
from typing import Any

from ..payloads.token_estimation import (
    estimateSingleMessageTokens,
    estimateTextTokens,
    stringifyValueForTokenEstimate,
)
from ..providers.openai import buildChatCompletionsCreateParams, buildResponsesCreateParams


def _estimate_request_tokens(request: dict[str, Any], image_input_tokens: int = 0) -> int:
    # role:content per message, like every other estimator (TS parity):
    # serializing whole message dicts with spaced JSON inflated
    # metadata-bearing tool results by 43.5% against the compaction units
    # this number is subtracted from.
    total = estimateTextTokens(request.get("system", ""))
    for message in request.get("messages", []):
        total += estimateSingleMessageTokens(message)
    if request.get("tools"):
        total += estimateTextTokens(stringifyValueForTokenEstimate(request["tools"]))
    return total + image_input_tokens


def _estimate_openai_request_tokens(request: dict[str, Any], mode: str, image_input_tokens: int = 0) -> int:
    if mode == "chat_completions":
        params = buildChatCompletionsCreateParams(request)
        total = 0
        for message in params.get("messages") or []:
            total += estimateTextTokens(stringifyValueForTokenEstimate(message))
        if params.get("tools"):
            total += estimateTextTokens(stringifyValueForTokenEstimate(params["tools"]))
        if params.get("reasoning_effort"):
            total += estimateTextTokens(json.dumps(params["reasoning_effort"], ensure_ascii=False))
        if params.get("stream_options"):
            total += estimateTextTokens(json.dumps(params["stream_options"], ensure_ascii=False))
        return total + image_input_tokens
    params = _sanitize_openai_count_params(
        buildResponsesCreateParams({"isCodexBackend": mode == "codex", "request": request})
    )
    total = 0
    if isinstance(params.get("instructions"), str):
        total += estimateTextTokens(params["instructions"])
    input_value = params.get("input")
    if isinstance(input_value, list):
        for item in input_value:
            total += estimateTextTokens(stringifyValueForTokenEstimate(item))
    elif isinstance(input_value, str):
        total += estimateTextTokens(input_value)
    if params.get("tools"):
        total += estimateTextTokens(stringifyValueForTokenEstimate(params["tools"]))
    if params.get("reasoning"):
        total += estimateTextTokens(stringifyValueForTokenEstimate(params["reasoning"]))
    return total + image_input_tokens


def _sanitize_openai_count_params(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(value)
    for key in ("stream", "store", "max_output_tokens", "prompt_cache_key", "prompt_cache_retention"):
        sanitized.pop(key, None)
    return sanitized
