from __future__ import annotations

from .._utils import signal_aborted as _signal_aborted
from ._knob_reader import context_knob as _context_knob

import asyncio
import importlib as _importlib
import json
from typing import Any
from urllib.request import Request, urlopen

from .._utils import get_value as _get_value, maybe_await as _maybe_await, now_iso
from ..payloads.image_budget import measure_admitted_image_set as _measure_admitted_image_set
from ..payloads.image_redaction import redact_image_payloads as _redact_image_payloads
from .request_estimation import (
    _estimate_openai_request_tokens,
    _estimate_request_tokens,
)
from ..payloads.token_estimation import estimate_text_tokens
from ..providers.errors import (
    create_provider_failure_record as _create_provider_failure_record,
    create_provider_request_error as _create_provider_request_error,
    is_provider_request_error as _is_provider_request_error,
)
from ..providers.watchdog import create_provider_request_watchdog as _create_provider_request_watchdog
from ..providers.anthropic import build_anthropic_count_tokens_payload, build_anthropic_headers, resolve_anthropic_count_tokens_url
from ..providers.openai import build_chat_completions_create_params, build_responses_create_params, resolve_openai_base_url as _resolve_openai_base_url
from ..runtime.secret_provider import resolve_provider_config_secrets

MAX_CODEX_ERROR_SAMPLES = 12

async def measure_request_input_tokens(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    config = args["config"]
    request = args["request"]
    measured_at = now_iso()
    images = _measure_admitted_image_set(request.get("messages") or [], config.get("provider") or "openai")
    image_fields = _image_measurement_fields(images)
    if config.get("provider") == "anthropic":
        try:
            resolved_config = await resolve_provider_config_secrets(config)
            input_tokens = await _run_exact_token_count(
                config=config,
                model=request["model"],
                abort_signal=args.get("abortSignal"),
                deadlines=args.get("deadlines"),
                timeout_ms=_configured_token_count_timeout_ms(args),
                operation=lambda signal: _measure_anthropic_count_tokens(
                    resolved_config,
                    request,
                    _configured_token_count_timeout_ms(args) / 1000,
                    signal,
                ),
            )
            return {
                "inputTokens": input_tokens,
                "source": "anthropic_count", "exactCountFallback": False,
                "measuredAt": measured_at,
                "provider": "anthropic",
                "model": request["model"],
                **_request_component_fields(request, config, images["estimatedInputTokens"]),
                **image_fields,
            }
        except Exception as error:
            _rethrow_parent_abort(args.get("abortSignal"), error)
            base_estimate = _estimate_request_tokens(request, images["estimatedInputTokens"])
            estimate = base_estimate + _context_knob("context.estimate_margin_tokens")
            if _exact_token_count_required(config):
                raise _normalize_mandatory_exact_failure(config, request["model"], error)
            _emit_token_count_fallback(
                args=args,
                estimate=estimate,
                source="legacy_estimate",
                error=error,
            )
            return {
                "inputTokens": estimate,
                "baseInputTokens": base_estimate,
                "safetyMarginTokens": _context_knob("context.estimate_margin_tokens"),
                "source": "legacy_estimate", "exactCountFallback": True,
                "measuredAt": measured_at,
                "provider": "anthropic",
                "model": request["model"],
                **_request_component_fields(request, config, images["estimatedInputTokens"]),
                **image_fields,
            }
    mode = config.get("openaiApiMode", "responses")
    exact_count_fallback = False
    resolved_config = await resolve_provider_config_secrets(config)
    if mode == "responses" and resolved_config.get("openaiToken"):
        try:
            exact = await _run_exact_token_count(
                config=config,
                model=request["model"],
                abort_signal=args.get("abortSignal"),
                deadlines=args.get("deadlines"),
                timeout_ms=_configured_token_count_timeout_ms(args),
                operation=lambda signal: _measure_openai_exact_tokens(
                    resolved_config,
                    request,
                    signal,
                ),
            )
            return {
                "inputTokens": exact,
                "source": "openai_exact", "exactCountFallback": False,
                "measuredAt": measured_at,
                "provider": "openai",
                "model": request["model"],
                **_request_component_fields(request, config, images["estimatedInputTokens"]),
                **image_fields,
            }
        except Exception as error:
            _rethrow_parent_abort(args.get("abortSignal"), error)
            if _exact_token_count_required(config):
                raise _normalize_mandatory_exact_failure(config, request["model"], error)
            exact_count_fallback = True
            fallback_base = _estimate_openai_request_tokens(
                request, mode, images["estimatedInputTokens"]
            )
            _emit_token_count_fallback(
                args=args,
                estimate=fallback_base + _context_knob("context.estimate_margin_tokens"),
                source=_openai_estimate_source(mode),
                error=error,
            )
    elif _exact_token_count_required(config):
        raise _normalize_mandatory_exact_failure(
            config,
            request["model"],
            RuntimeError(f"Exact token counting is unavailable for OpenAI mode '{mode}'."),
        )
    base = _estimate_openai_request_tokens(request, mode, images["estimatedInputTokens"])
    margin = (
        _compute_codex_safety_margin(args.get("session", {}))
        if mode == "codex"
        else _context_knob("context.estimate_margin_tokens")
        if exact_count_fallback
        else 0
    )
    return {
        "inputTokens": base + margin,
        "baseInputTokens": base,
        "safetyMarginTokens": margin or None,
        "source": _openai_estimate_source(mode), "exactCountFallback": exact_count_fallback,
        "measuredAt": measured_at,
        "provider": "openai",
        "model": request["model"],
        **_request_component_fields(request, config, images["estimatedInputTokens"]),
        **image_fields,
    }

def record_preflight_token_measurement(session: dict[str, Any], measurement: dict[str, Any]) -> None:
    tracking = session.setdefault("metadata", {}).setdefault("tokenTracking", {})
    tracking["lastPreflight"] = measurement
    if session.get("metadata", {}).get("compactionState"):
        session["metadata"]["compactionState"]["workingContextTokenMeasurement"] = measurement
        session["metadata"]["compactionState"]["workingContextTokenEstimate"] = measurement["inputTokens"]

def record_actual_token_usage(session: dict[str, Any], usage: dict[str, Any]) -> None:
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
    return estimate_text_tokens(value)


def _request_component_fields(
    request: dict[str, Any],
    config: dict[str, Any],
    image_input_tokens: int,
) -> dict[str, int]:
    without_messages = {**request, "messages": []}
    if config.get("provider") == "openai":
        mode = config.get("openaiApiMode", "responses")
        fixed = _estimate_openai_request_tokens(without_messages, mode, 0)
        complete = _estimate_openai_request_tokens(request, mode, image_input_tokens)
    else:
        fixed = _estimate_request_tokens(without_messages, 0)
        complete = _estimate_request_tokens(request, image_input_tokens)
    text_messages = max(0, complete - fixed - max(0, int(image_input_tokens or 0)))
    return {
        "estimatedFixedInputTokens": fixed,
        "estimatedMessageInputTokens": text_messages,
        "estimatedImageInputTokens": max(0, int(image_input_tokens or 0)),
    }


async def _measure_anthropic_count_tokens(
    config: dict[str, Any],
    request: dict[str, Any],
    timeout_seconds: float,
    _abort_signal: Any = None,
) -> int:
    payload = build_anthropic_count_tokens_payload(request, {"config": config})

    def send() -> int:
        req = Request(
            resolve_anthropic_count_tokens_url(config.get("anthropicBaseUrl")),
            data=json.dumps(payload).encode("utf-8"),
            headers=build_anthropic_headers(config),
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


async def _measure_openai_exact_tokens(
    config: dict[str, Any],
    request: dict[str, Any],
    abort_signal: Any,
) -> int:
    factory = config.get("openaiTokenCountClientFactory")
    client: Any
    if callable(factory):
        client = factory(
            {
                "apiKey": config.get("openaiToken"),
                "baseURL": _resolve_openai_base_url("responses", config.get("openaiBaseUrl")),
                "abortSignal": abort_signal,
            }
        )
    else:
        try:
            openai_module = _importlib.import_module("openai")
        except Exception as exc:
            raise RuntimeError("OpenAI exact token counting requires the optional OpenAI SDK.") from exc
        async_openai = getattr(openai_module, "AsyncOpenAI", None)
        if async_openai is None:
            raise RuntimeError("OpenAI exact token counting is unavailable in this OpenAI SDK.")
        client_kwargs: dict[str, Any] = {"api_key": config.get("openaiToken")}
        base_url = _resolve_openai_base_url("responses", config.get("openaiBaseUrl"))
        if base_url:
            client_kwargs["base_url"] = base_url
        client = async_openai(**client_kwargs)
    responses = _get_value(client, "responses")
    input_tokens_api = _get_value(responses, "input_tokens") or _get_value(responses, "inputTokens")
    count = _get_value(input_tokens_api, "count")
    if not callable(count):
        raise RuntimeError("OpenAI input-token count endpoint is unavailable.")
    params = _sanitize_openai_count_params(
        build_responses_create_params({"isCodexBackend": False, "request": request})
    )
    if callable(factory):
        response = await _maybe_await(count(params))
    else:
        response = await _maybe_await(count(**params))
    input_tokens = _get_value(response, "input_tokens")
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int):
        raise RuntimeError("OpenAI input-token count returned no input_tokens value.")
    return input_tokens


def _sanitize_openai_count_params(value: dict[str, Any]) -> dict[str, Any]:
    next_value = dict(value)
    for key in (
        "stream",
        "store",
        "max_output_tokens",
        "prompt_cache_key",
        "prompt_cache_retention",
    ):
        next_value.pop(key, None)
    return next_value


async def _run_exact_token_count(
    *,
    config: dict[str, Any],
    model: str,
    abort_signal: Any,
    deadlines: Any,
    timeout_ms: int,
    operation: Any,
) -> int:
    configured_timeout_ms = max(1, int(timeout_ms or _context_knob("provider.token_count_timeout_ms")))
    remaining_ms = _deadline_remaining_ms(deadlines)
    effective_timeout_ms = (
        max(1, min(configured_timeout_ms, remaining_ms))
        if remaining_ms is not None
        else configured_timeout_ms
    )
    watchdog = _create_provider_request_watchdog(
        parentSignal=abort_signal,
        initialTimeoutMs=effective_timeout_ms,
        buildTimeoutError=lambda: _create_provider_request_error(
            _create_provider_failure_record(
                provider=config.get("provider"),
                providerLabel=config.get("providerProfileName") or config.get("provider"),
                phase="token_count",
                category="timeout",
                retryable=True,
                attempt=1,
                hadAssistantOutput=False,
                hadToolUse=False,
                rawMessage=f"Exact token count timed out after {effective_timeout_ms}ms.",
            )
        ),
    )
    try:
        result = await watchdog.race(operation(watchdog.signal))
    finally:
        watchdog.dispose()
    if isinstance(result, bool) or not isinstance(result, int):
        raise RuntimeError(f"Exact token count for model '{model}' was not an integer.")
    return result


def _configured_token_count_timeout_ms(args: dict[str, Any]) -> int:
    explicit_seconds = args.get("timeoutSeconds")
    if isinstance(explicit_seconds, (int, float)) and not isinstance(explicit_seconds, bool) and explicit_seconds > 0:
        return max(1, int(explicit_seconds * 1000))
    deadlines = args.get("deadlines")
    deadline_timeout = _deadline_value(deadlines, "tokenCountTimeoutMs")
    profile = (args.get("config") or {}).get("providerProfile") or {}
    return (
        _positive_timeout(deadline_timeout)
        or _positive_timeout(profile.get("tokenCountTimeoutMs"))
        or _context_knob("provider.token_count_timeout_ms")
    )


def _deadline_remaining_ms(deadlines: Any) -> int | None:
    if deadlines is None:
        return None
    remaining = _deadline_value(deadlines, "remainingMs")
    if callable(remaining):
        try:
            return max(1, int(remaining()))
        except Exception:
            return 1
    return None


def _deadline_value(deadlines: Any, key: str) -> Any:
    if isinstance(deadlines, dict):
        return deadlines.get(key)
    return getattr(deadlines, key, None)


def _positive_timeout(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _exact_token_count_required(config: dict[str, Any]) -> bool:
    return (config.get("providerProfile") or {}).get("exactTokenCountRequired") is True


def _normalize_mandatory_exact_failure(
    config: dict[str, Any],
    model: str,
    error: BaseException,
) -> BaseException:
    if _is_provider_request_error(error):
        record = dict(error.record)
        record.update(
            {
                "phase": "token_count",
                "retryable": False,
                "rawMessage": _bounded_failure_reason(error),
            }
        )
        return _create_provider_request_error(record)
    return _create_provider_request_error(
        _create_provider_failure_record(
            provider=config.get("provider"),
            providerLabel=config.get("providerProfileName") or config.get("provider"),
            phase="token_count",
            category="protocol",
            retryable=False,
            attempt=1,
            hadAssistantOutput=False,
            hadToolUse=False,
            rawMessage=(
                f"Exact token count is required for model '{model}' but failed: "
                f"{_bounded_failure_reason(error)}"
            ),
        )
    )


def _emit_token_count_fallback(
    *,
    args: dict[str, Any],
    estimate: int,
    source: str,
    error: BaseException,
) -> None:
    emit = args.get("emitEvent")
    if not callable(emit):
        return
    config = args["config"]
    session = args.get("session") or {}
    try:
        emit(
            "provider.token_count_fallback",
            {
                "session_id": session.get("id"),
                "provider": config.get("provider"),
                "provider_profile": config.get("providerProfileName"),
                "model": args["request"]["model"],
                "phase": "token_count",
                "reason": error.record.get("category") if _is_provider_request_error(error) else "unavailable",
                "detail": _bounded_failure_reason(error),
                "token_count_timeout_ms": _configured_token_count_timeout_ms(args),
                "exact_required": False,
                "fallback_source": source,
                "estimated_input_tokens": estimate,
            },
        )
    except Exception:
        pass


def _rethrow_parent_abort(signal: Any, error: BaseException) -> None:
    if not _signal_aborted(signal):
        return
    reason = signal.get("reason") if isinstance(signal, dict) else getattr(signal, "reason", None)
    raise reason if isinstance(reason, BaseException) else error




def _bounded_failure_reason(error: BaseException) -> str:
    if _is_provider_request_error(error):
        value = error.record.get("rawMessage") or str(error)
    else:
        value = str(error)
    normalized = " ".join(str(value).replace("\t", " ").splitlines()).strip()
    return normalized if len(normalized) <= 256 else f"{normalized[:255]}…"


def _openai_estimate_source(mode: str) -> str:
    if mode == "codex":
        return "codex_estimate"
    if mode == "chat_completions":
        return "openai_chat_estimate"
    return "openai_responses_estimate"




def _image_measurement_fields(images: dict[str, Any]) -> dict[str, Any]:
    if not images["count"]:
        return {}
    return {
        "imageCount": images["count"],
        "imageDecodedBytes": images["decodedBytes"],
        "imageEncodedRequestBytes": images["encodedRequestBytes"],
        "imageInputTokens": images["estimatedInputTokens"],
        "imageSetFingerprint": images["fingerprint"],
        "imageMeasurements": images["images"],
    }


def _compute_codex_safety_margin(session: dict[str, Any]) -> int:
    errors = session.get("metadata", {}).get("tokenTracking", {}).get("calibration", {}).get("codexPositiveErrors", [])
    if not errors:
        return _context_knob("context.estimate_margin_tokens")
    sorted_errors = sorted(errors)
    index = min(len(sorted_errors) - 1, max(0, int(len(sorted_errors) * 0.95) - 1))
    return max(_context_knob("context.estimate_margin_tokens"), sorted_errors[index])
