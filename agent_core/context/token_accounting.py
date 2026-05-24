from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.request import Request, urlopen

from .._utils import now_iso
from ..providers.anthropic import buildAnthropicCountTokensPayload, buildAnthropicHeaders, resolveAnthropicCountTokensUrl
from ..providers.openai import buildChatCompletionsCreateParams, buildResponsesCreateParams


DEFAULT_ESTIMATE_MARGIN_TOKENS = 2000
MAX_CODEX_ERROR_SAMPLES = 12


async def measureRequestInputTokens(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    config = args["config"]
    request = args["request"]
    measured_at = now_iso()
    if config.get("provider") == "anthropic":
        try:
            input_tokens = await _measure_anthropic_count_tokens(config, request, float(args.get("timeoutSeconds") or 30))
            return {"inputTokens": input_tokens, "source": "anthropic_count", "measuredAt": measured_at, "provider": "anthropic", "model": request["model"]}
        except Exception:
            estimate = _estimate_request_tokens(request)
            return {"inputTokens": estimate, "baseInputTokens": estimate, "source": "legacy_estimate", "measuredAt": measured_at, "provider": "anthropic", "model": request["model"]}
    mode = config.get("openaiApiMode", "responses")
    base = _estimate_openai_request_tokens(request, mode)
    margin = _compute_codex_safety_margin(args.get("session", {})) if mode == "codex" else 0
    return {"inputTokens": base + margin, "baseInputTokens": base, "safetyMarginTokens": margin or None, "source": "codex_estimate" if mode == "codex" else "openai_chat_estimate" if mode == "chat_completions" else "openai_responses_estimate", "measuredAt": measured_at, "provider": "openai", "model": request["model"]}


def recordPreflightTokenMeasurement(session: dict[str, Any], measurement: dict[str, Any]) -> None:
    tracking = session.setdefault("metadata", {}).setdefault("tokenTracking", {})
    tracking["lastPreflight"] = measurement
    if session.get("metadata", {}).get("compactionState"):
        session["metadata"]["compactionState"]["workingContextTokenMeasurement"] = measurement
        session["metadata"]["compactionState"]["workingContextTokenEstimate"] = measurement["inputTokens"]


def recordActualTokenUsage(session: dict[str, Any], usage: dict[str, Any]) -> None:
    tracking = session.setdefault("metadata", {}).setdefault("tokenTracking", {})
    if usage.get("input_tokens") is not None:
        tracking["lastActualInputTokens"] = usage.get("input_tokens")
    if usage.get("output_tokens") is not None:
        tracking["lastActualOutputTokens"] = usage.get("output_tokens")
    preflight = tracking.get("lastPreflight")
    if not preflight or preflight.get("source") != "codex_estimate" or not isinstance(preflight.get("baseInputTokens"), int):
        return
    actual = usage.get("input_tokens")
    if not isinstance(actual, int):
        return
    delta = actual - preflight["baseInputTokens"]
    tracking["lastEstimateDelta"] = delta
    calibration = tracking.setdefault("calibration", {"codexPositiveErrors": []})
    calibration["codexPositiveErrors"] = [*calibration.get("codexPositiveErrors", []), max(0, delta)][-MAX_CODEX_ERROR_SAMPLES:]


def _estimate_text_tokens(value: str) -> int:
    return (len(value) + 3) // 4


def _estimate_request_tokens(request: dict[str, Any]) -> int:
    total = _estimate_text_tokens(request.get("system", ""))
    for message in request.get("messages", []):
        total += _estimate_text_tokens(json.dumps(_redact_image_payloads(message), ensure_ascii=False))
    if request.get("tools"):
        total += _estimate_text_tokens(json.dumps(request["tools"], ensure_ascii=False))
    return total


async def _measure_anthropic_count_tokens(config: dict[str, Any], request: dict[str, Any], timeout_seconds: float) -> int:
    payload = buildAnthropicCountTokensPayload(request, {"config": config})

    def send() -> int:
        req = Request(
            resolveAnthropicCountTokensUrl(config.get("anthropicBaseUrl")),
            data=json.dumps(payload).encode("utf-8"),
            headers=buildAnthropicHeaders(config),
            method="POST",
        )
        with urlopen(req, timeout=timeout_seconds) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Anthropic count_tokens failed with status {response.status}")
            parsed = json.loads(response.read().decode("utf-8"))
        input_tokens = parsed.get("input_tokens")
        if not isinstance(input_tokens, int):
            raise RuntimeError("Anthropic count_tokens returned no input_tokens value.")
        return input_tokens

    return await asyncio.to_thread(send)


def _estimate_openai_request_tokens(request: dict[str, Any], mode: str) -> int:
    params = buildChatCompletionsCreateParams(request) if mode == "chat_completions" else buildResponsesCreateParams({"isCodexBackend": mode == "codex", "request": request})
    params.pop("stream", None)
    params.pop("store", None)
    params.pop("max_output_tokens", None)
    params = _redact_image_payloads(params)
    return _estimate_text_tokens(json.dumps(params, ensure_ascii=False))


def _redact_image_payloads(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_image_payloads(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    is_image = str(value.get("type") or "").strip().lower() in {"image", "input_image", "image_url"}
    for key, item in value.items():
        if is_image and key in {"data", "data_base64", "base64", "image_url", "url"}:
            out[key] = _image_placeholder(value)
        else:
            out[key] = _redact_image_payloads(item)
    return out


def _image_placeholder(value: dict[str, Any]) -> str:
    mime_type = value.get("mime_type") or value.get("mimeType") or value.get("mime") or "image"
    return f"<image:{mime_type}>"


def _compute_codex_safety_margin(session: dict[str, Any]) -> int:
    errors = session.get("metadata", {}).get("tokenTracking", {}).get("calibration", {}).get("codexPositiveErrors", [])
    if not errors:
        return DEFAULT_ESTIMATE_MARGIN_TOKENS
    sorted_errors = sorted(errors)
    index = min(len(sorted_errors) - 1, max(0, int(len(sorted_errors) * 0.95) - 1))
    return max(DEFAULT_ESTIMATE_MARGIN_TOKENS, sorted_errors[index])
