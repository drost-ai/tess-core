from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .turn_session import ProviderTurnSession, _close_resource

_INTERNAL_ITEM_KEYS = {"_tess_core", "provider_metadata"}
_TURN_STATE_HEADER = "x-codex-turn-state"


@dataclass
class PreparedOpenAiRequest:
    logical_params: dict[str, Any]
    wire_params: dict[str, Any]
    full_replay_params: dict[str, Any]
    projection: str
    transport: str
    chain_reset_reason: str | None
    logical_input_bytes: int
    logical_input_items: int
    transmitted_input_bytes: int
    transmitted_input_items: int
    previous_response_id_present: bool
    connection_reused: bool


class OpenAiProviderTurnSession(ProviderTurnSession):
    def __init__(self, *, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        capabilities = (config.get("providerProfile") or {}).get("capabilities") or {}
        self._route_capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
        self._client: Any = None
        self._client_identity: Any = None
        self._client_capabilities: dict[str, Any] = {}
        self._last_logical_params: dict[str, Any] | None = None
        self._last_logical_input: list[Any] | None = None
        self._last_full_replay_input: list[Any] | None = None
        self._last_canonical_output: list[Any] = []
        self._last_output_items: list[Any] = []
        self._last_response_id: str | None = None
        self._turn_state: str | None = None
        self._websocket_disabled_reason: str | None = None
        self._created_at = time.monotonic()
        self._first_provider_activity_at: float | None = None
        self._first_assistant_text_at: float | None = None
        self._completed_at: float | None = None
        self._last_provider_activity_at: float | None = None

    async def acquire_client(
        self,
        *,
        identity: Any,
        create: Callable[[], Any],
    ) -> tuple[Any, bool]:
        if self._client is not None and identity == self._client_identity:
            return self._client, True
        if self._client is not None:
            await _close_resource(self._client)
            self._clear_chain("client_identity_changed")
        self._client = create()
        self._client_identity = copy.deepcopy(identity)
        capabilities = getattr(self._client, "capabilities", None)
        self._client_capabilities = (
            dict(capabilities) if isinstance(capabilities, dict) else {}
        )
        return self._client, False

    def prepare_responses_request(
        self,
        logical_params: dict[str, Any],
        *,
        connection_reused: bool,
    ) -> PreparedOpenAiRequest:
        logical = copy.deepcopy(logical_params)
        logical.pop("previous_response_id", None)
        logical_input = _input_items(logical)
        full_replay = copy.deepcopy(logical)
        full_replay_input = copy.deepcopy(logical_input)
        suffix: list[Any] | None = None
        reset_reason: str | None = None
        properties_match = False

        if self._last_logical_params is None:
            reset_reason = "first_request"
        elif not _request_properties_match(self._last_logical_params, logical):
            reset_reason = "request_properties_changed"
            suffix = self._extension_suffix(logical_input)
        else:
            properties_match = True
            suffix = self._extension_suffix(logical_input)
            if suffix is None:
                reset_reason = "input_prefix_mismatch"
            elif not suffix and not self._allow_empty_delta():
                reset_reason = "empty_delta_not_supported"

        if suffix is not None and self._last_full_replay_input is not None:
            full_replay_input = [
                *copy.deepcopy(self._last_full_replay_input),
                *copy.deepcopy(self._last_output_items),
                *copy.deepcopy(suffix),
            ]
            full_replay["input"] = full_replay_input

        incremental = bool(
            properties_match
            and suffix is not None
            and (suffix or self._allow_empty_delta())
            and self._last_response_id
            and self._incremental_supported()
        )
        wire = copy.deepcopy(full_replay)
        projection = "full"
        if incremental:
            wire["input"] = copy.deepcopy(suffix)
            wire["previous_response_id"] = self._last_response_id
            projection = "incremental"
            reset_reason = None

        transport = "websocket" if self._websocket_supported() else "sse"
        if self._turn_state and self._sticky_turn_state_supported():
            headers = dict(wire.get("extra_headers") or {})
            headers[_TURN_STATE_HEADER] = self._turn_state
            wire["extra_headers"] = headers
            replay_headers = dict(full_replay.get("extra_headers") or {})
            replay_headers[_TURN_STATE_HEADER] = self._turn_state
            full_replay["extra_headers"] = replay_headers

        return PreparedOpenAiRequest(
            logical_params=logical,
            wire_params=wire,
            full_replay_params=full_replay,
            projection=projection,
            transport=transport,
            chain_reset_reason=reset_reason,
            logical_input_bytes=_json_bytes(logical_input),
            logical_input_items=len(logical_input),
            transmitted_input_bytes=_json_bytes(_input_items(wire)),
            transmitted_input_items=len(_input_items(wire)),
            previous_response_id_present=bool(wire.get("previous_response_id")),
            connection_reused=connection_reused,
        )

    async def record_responses_success(
        self,
        prepared: PreparedOpenAiRequest,
        *,
        output_items: list[Any],
        canonical_output_items: list[Any],
        response_id: str | None,
        turn_state: str | None,
    ) -> None:
        self._last_logical_params = copy.deepcopy(prepared.logical_params)
        self._last_logical_input = copy.deepcopy(_input_items(prepared.logical_params))
        self._last_full_replay_input = copy.deepcopy(
            _input_items(prepared.full_replay_params)
        )
        self._last_output_items = _complete_replay_output_items(
            output_items,
            canonical_output_items,
        )
        self._last_canonical_output = copy.deepcopy(canonical_output_items)
        self._last_response_id = (
            response_id.strip() or None if isinstance(response_id, str) else None
        )
        if isinstance(turn_state, str) and turn_state.strip() and not self._turn_state:
            self._turn_state = turn_state.strip()
        self._completed_at = time.monotonic()

    async def record_responses_failure(
        self, prepared: PreparedOpenAiRequest, failure: BaseException
    ) -> None:
        self._clear_chain("generation_failed")

    def fallback_to_sse(
        self, prepared: PreparedOpenAiRequest, reason: str
    ) -> PreparedOpenAiRequest:
        self._websocket_disabled_reason = reason or "websocket_rejected"
        wire = copy.deepcopy(prepared.full_replay_params)
        return PreparedOpenAiRequest(
            logical_params=prepared.logical_params,
            wire_params=wire,
            full_replay_params=prepared.full_replay_params,
            projection="full",
            transport="sse",
            chain_reset_reason=f"websocket_fallback:{self._websocket_disabled_reason}",
            logical_input_bytes=prepared.logical_input_bytes,
            logical_input_items=prepared.logical_input_items,
            transmitted_input_bytes=_json_bytes(_input_items(wire)),
            transmitted_input_items=len(_input_items(wire)),
            previous_response_id_present=False,
            connection_reused=prepared.connection_reused,
        )

    def mark_provider_activity(self, kind: str) -> None:
        now = time.monotonic()
        self._last_provider_activity_at = now
        if self._first_provider_activity_at is None:
            self._first_provider_activity_at = now
        if kind == "output_delta" and self._first_assistant_text_at is None:
            self._first_assistant_text_at = now

    def latency_telemetry(self) -> dict[str, int | None]:
        return {
            "first_provider_activity_ms": _elapsed_ms(
                self._created_at, self._first_provider_activity_at
            ),
            "first_assistant_text_ms": _elapsed_ms(
                self._created_at, self._first_assistant_text_at
            ),
            "completion_ms": _elapsed_ms(self._created_at, self._completed_at),
        }

    async def close(self) -> None:
        if self._closed:
            return
        await super().close()
        client = self._client
        self._client = None
        await _close_resource(client)
        self._clear_chain("turn_closed")

    def _extension_suffix(self, current_input: list[Any]) -> list[Any] | None:
        if self._last_logical_input is None:
            return None
        expected = [*self._last_logical_input, *self._last_canonical_output]
        if len(current_input) < len(expected):
            return None
        if not all(
            _items_equal(previous, current)
            for previous, current in zip(expected, current_input)
        ):
            return None
        return copy.deepcopy(current_input[len(expected) :])

    def _incremental_supported(self) -> bool:
        return bool(
            self._route_capabilities.get("openaiResponsesIncremental")
            and self._client_capabilities.get("openaiResponsesIncremental")
            and self._websocket_supported()
        )

    def _websocket_supported(self) -> bool:
        return bool(
            not self._websocket_disabled_reason
            and self._route_capabilities.get("openaiResponsesWebSocket")
            and self._client_capabilities.get("openaiResponsesWebSocket")
        )

    def _sticky_turn_state_supported(self) -> bool:
        return bool(
            self._route_capabilities.get("openaiTurnState")
            and self._client_capabilities.get("openaiTurnState")
        )

    def _allow_empty_delta(self) -> bool:
        return bool(
            self._route_capabilities.get("openaiResponsesEmptyDelta")
            and self._client_capabilities.get("openaiResponsesEmptyDelta")
        )

    def _clear_chain(self, _reason: str) -> None:
        self._last_logical_params = None
        self._last_logical_input = None
        self._last_full_replay_input = None
        self._last_canonical_output = []
        self._last_output_items = []
        self._last_response_id = None
        self._turn_state = None


def websocket_fallback_safe(error: BaseException) -> bool:
    return bool(getattr(error, "fallback_safe", False))


def websocket_failure_reason(error: BaseException) -> str:
    stage = str(getattr(error, "stage", "") or "").strip()
    return stage or error.__class__.__name__.lower()


def _request_properties_match(
    previous: dict[str, Any], current: dict[str, Any]
) -> bool:
    return _comparable_properties(previous) == _comparable_properties(current)


def _comparable_properties(params: dict[str, Any]) -> dict[str, Any]:
    comparable = copy.deepcopy(params)
    comparable.pop("input", None)
    comparable.pop("previous_response_id", None)
    headers = comparable.get("extra_headers")
    if isinstance(headers, dict):
        cleaned = {
            key: value
            for key, value in headers.items()
            if str(key).strip().lower() != _TURN_STATE_HEADER
        }
        if cleaned:
            comparable["extra_headers"] = cleaned
        else:
            comparable.pop("extra_headers", None)
    return comparable


def _items_equal(previous: Any, current: Any) -> bool:
    return _without_internal_metadata(previous) == _without_internal_metadata(current)


def _without_internal_metadata(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_internal_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _without_internal_metadata(item)
        for key, item in value.items()
        if key not in _INTERNAL_ITEM_KEYS
    }


def _input_items(params: dict[str, Any]) -> list[Any]:
    value = params.get("input")
    return value if isinstance(value, list) else []


def _complete_replay_output_items(
    output_items: list[Any],
    canonical_output_items: list[Any],
) -> list[Any]:
    """Keep full replay executable when a terminal stream omits raw output.

    Some Responses transports stream complete function-call items but leave the
    terminal ``response.output`` collection empty.  The agent loop still has the
    canonical assistant projection, so use it to fill only missing executable
    items.  Raw reasoning and message items remain authoritative when present.
    """

    replay = copy.deepcopy(output_items)
    raw_call_ids = {
        str(item.get("call_id") or "")
        for item in replay
        if isinstance(item, dict) and item.get("type") == "function_call"
    }
    has_message = any(
        isinstance(item, dict) and item.get("type") == "message" for item in replay
    )
    for item in canonical_output_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = str(item.get("call_id") or "")
            if call_id and call_id not in raw_call_ids:
                replay.append(copy.deepcopy(item))
                raw_call_ids.add(call_id)
        elif item_type == "message" and not has_message:
            replay.append(copy.deepcopy(item))
            has_message = True
    return replay


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    )


def _elapsed_ms(started: float, finished: float | None) -> int | None:
    if finished is None:
        return None
    return max(0, int((finished - started) * 1000))
