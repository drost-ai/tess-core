from __future__ import annotations

import copy

from ..openai_turn_session import (
    PreparedOpenAiRequest as _PreparedOpenAiRequest,
    websocket_failure_reason,
    websocket_fallback_safe,
)

from .shared import *
from .transport import _emit, _race_openai_activity

_WEBSOCKET_RESPONSE_CREATE_FIELDS = {
    "model",
    "instructions",
    "previous_response_id",
    "input",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "reasoning",
    "store",
    "stream",
    "stream_options",
    "include",
    "service_tier",
    "prompt_cache_key",
    "text",
    "generate",
    "client_metadata",
}


async def start_responses_stream(
    *,
    responses: Any,
    prepared: _PreparedOpenAiRequest,
    runtime: dict[str, Any],
    turn_session: Any,
    provider_label: str,
    model: str,
    attempt: int,
    timeout_ms: int,
) -> tuple[Any, _PreparedOpenAiRequest]:
    selected = prepared
    if prepared.transport == "websocket":
        websocket_create = get_value(responses, "create_websocket")
        if callable(websocket_create):
            try:
                stream = await _race_openai_activity(
                    maybe_await(
                        websocket_create(
                            websocket_response_create_params(prepared.wire_params)
                        )
                    ),
                    timeout_ms=timeout_ms,
                    provider_label=provider_label,
                    phase="request_start",
                    attempt=attempt,
                    had_output=False,
                    had_tool_use=False,
                    abort_signal=runtime.get("abortSignal"),
                    turn_session=turn_session,
                )
                return stream, selected
            except Exception as exc:
                if turn_session is None or not websocket_fallback_safe(exc):
                    raise
                reason = websocket_failure_reason(exc)
                selected = turn_session.fallback_to_sse(prepared, reason)
                _emit(
                    runtime,
                    "provider.transport_fallback",
                    {
                        "provider": "openai",
                        "provider_label": provider_label,
                        "model": model,
                        "attempt": attempt,
                        "from_transport": "websocket",
                        "to_transport": "sse",
                        "reason": reason,
                        **projection_event_fields(selected),
                    },
                )
        else:
            selected = turn_session.fallback_to_sse(
                prepared, "websocket_method_unavailable"
            )
    create = get_value(responses, "create")
    if not callable(create):
        raise RuntimeError("OpenAI Responses create method is unavailable.")
    stream = await _race_openai_activity(
        maybe_await(create(selected.wire_params)),
        timeout_ms=timeout_ms,
        provider_label=provider_label,
        phase="request_start",
        attempt=attempt,
        had_output=False,
        had_tool_use=False,
        abort_signal=runtime.get("abortSignal"),
        turn_session=turn_session,
    )
    return stream, selected


def websocket_response_create_params(
    params: dict[str, Any],
) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in params.items()
        if key in _WEBSOCKET_RESPONSE_CREATE_FIELDS
    }


def stateless_prepared_request(
    logical_params: dict[str, Any], connection_reused: bool
) -> _PreparedOpenAiRequest:
    logical = copy.deepcopy(logical_params)
    input_items = logical.get("input") if isinstance(logical.get("input"), list) else []
    input_bytes = len(
        json.dumps(input_items, separators=(",", ":"), default=str).encode("utf-8")
    )
    return _PreparedOpenAiRequest(
        logical_params=logical,
        wire_params=copy.deepcopy(logical),
        full_replay_params=copy.deepcopy(logical),
        projection="full",
        transport="sse",
        chain_reset_reason="stateless",
        logical_input_bytes=input_bytes,
        logical_input_items=len(input_items),
        transmitted_input_bytes=input_bytes,
        transmitted_input_items=len(input_items),
        previous_response_id_present=False,
        connection_reused=connection_reused,
    )


def projection_event_fields(
    prepared: _PreparedOpenAiRequest,
) -> dict[str, Any]:
    return {
        "logical_input_bytes": prepared.logical_input_bytes,
        "logical_input_items": prepared.logical_input_items,
        "transmitted_input_bytes": prepared.transmitted_input_bytes,
        "transmitted_input_items": prepared.transmitted_input_items,
        "input_projection": prepared.projection,
        "previous_response_id_present": prepared.previous_response_id_present,
        "connection_reused": prepared.connection_reused,
        "transport": prepared.transport,
        "chain_reset_reason": prepared.chain_reset_reason,
    }


def responses_activity_kind(event_type: str) -> str:
    if event_type == "response.output_text.delta":
        return "output_delta"
    if event_type.startswith("response.reasoning"):
        return "reasoning"
    if event_type in {
        "response.output_item.added",
        "response.output_item.done",
        "response.function_call_arguments.done",
    }:
        return "tool_boundary"
    if event_type in {
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
        "error",
    }:
        return "terminal"
    return "stream_frame"


def extract_turn_state(value: Any) -> str | None:
    direct = get_value(value, "turn_state")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    headers = get_value(value, "headers")
    if headers is None:
        return None
    if isinstance(headers, dict):
        for key, item in headers.items():
            if str(key).strip().lower() == "x-codex-turn-state":
                cleaned = str(item or "").strip()
                return cleaned or None
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        item = getter("x-codex-turn-state")
        cleaned = str(item or "").strip()
        return cleaned or None
    return None
