from __future__ import annotations

import copy
import hashlib
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .._utils import maybe_await, now_iso
from ..storage import (
    read_json_file,
    session_archive_dir,
    with_record_transaction,
    write_json_file,
)


PARTIAL_OUTPUT_STATE_STORE_SCHEMA_VERSION = 1
PARTIAL_OUTPUT_STATE_SCHEMA_VERSION = 1
_TERMINAL_STATES = frozenset({"committed", "interrupted", "superseded"})
_EMPTY_TEXT_HASH = hashlib.sha256(b"").hexdigest()


class PartialOutputStateStore:
    schemaVersion = PARTIAL_OUTPUT_STATE_STORE_SCHEMA_VERSION

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.path = os.path.join(session_archive_dir(session_id), "partial-output-state.json")

    async def load(self) -> dict[str, Any] | None:
        value = await read_json_file(self.path, None)
        return sanitize_partial_output_state(value, expected_session_id=self.session_id)

    async def save(self, value: Mapping[str, Any]) -> bool:
        sanitized = sanitize_partial_output_state(
            value,
            expected_session_id=self.session_id,
        )
        if sanitized is None:
            return False
        async with with_record_transaction("partial-output", self.session_id):
            await write_json_file(self.path, sanitized)
        return True


def create_partial_output_state_store(session_id: str) -> PartialOutputStateStore:
    return PartialOutputStateStore(session_id)


def assert_partial_output_state_store(value: Any) -> None:
    if value is None:
        return
    if (
        state_store_value(value, "schemaVersion")
        != PARTIAL_OUTPUT_STATE_STORE_SCHEMA_VERSION
        or not callable(state_store_value(value, "load"))
        or not callable(state_store_value(value, "save"))
    ):
        raise ValueError("Partial output state store is incomplete or incompatible.")


def state_store_value(store: Any, name: str) -> Any:
    if isinstance(store, Mapping):
        return store.get(name)
    return getattr(store, name, None)


def partial_output_runtime_authority(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    binding = value.get("sessionBinding")
    binding = binding if isinstance(binding, Mapping) else value
    out = {
        key: binding.get(key)
        for key in ("generation", "configRevision", "runtimeInstanceId", "pathGraphId")
        if binding.get(key) is not None
    }
    if value.get("turnAuthorityId") is not None:
        out["turnAuthorityId"] = value["turnAuthorityId"]
    return out or None


def is_terminal_partial_output_state(value: Mapping[str, Any] | None) -> bool:
    return bool(value and value.get("terminalState") in _TERMINAL_STATES)


def sanitize_partial_output_state(
    value: Any,
    *,
    expected_session_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    session_id = _bounded_string(value.get("sessionId"), 256)
    logical_call_id = _bounded_string(value.get("logicalCallId"), 128)
    generation = _positive_integer(value.get("generation"))
    mode = value.get("deliveryMode")
    if (
        not session_id
        or not logical_call_id
        or generation is None
        or mode not in {"buffered", "replaceable", "append_only"}
        or (expected_session_id is not None and session_id != expected_session_id)
    ):
        return None
    terminal_state = value.get("terminalState")
    if terminal_state not in {*_TERMINAL_STATES, None}:
        terminal_state = None
    created_at = _bounded_string(value.get("createdAt"), 64) or now_iso()
    return {
        "schemaVersion": PARTIAL_OUTPUT_STATE_SCHEMA_VERSION,
        "sessionId": session_id,
        "logicalCallId": logical_call_id,
        "generation": generation,
        "deliveryMode": mode,
        "assistantTextHash": _bounded_string(value.get("assistantTextHash"), 128),
        "assistantTextLength": _nonnegative_integer(value.get("assistantTextLength")),
        "reasoningTextHash": _bounded_string(value.get("reasoningTextHash"), 128),
        "reasoningTextLength": _nonnegative_integer(value.get("reasoningTextLength")),
        "previewPublished": bool(value.get("previewPublished")),
        "responseId": _bounded_string(value.get("responseId"), 256),
        "sequenceNumber": _nonnegative_integer(value.get("sequenceNumber")),
        "supersessionRequested": bool(value.get("supersessionRequested")),
        "supersessionAcknowledged": bool(value.get("supersessionAcknowledged")),
        "recoveryAcknowledged": bool(value.get("recoveryAcknowledged")),
        "previewCommitAcknowledged": bool(value.get("previewCommitAcknowledged")),
        "terminalState": terminal_state,
        "runtimeAuthority": _runtime_authority(value.get("runtimeAuthority")),
        "createdAt": created_at,
        "updatedAt": _bounded_string(value.get("updatedAt"), 64) or now_iso(),
    }


def clone_partial_output_state(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


async def finalize_partial_output_state(
    store: Any,
    receipt: Any,
) -> bool:
    assert_partial_output_state_store(store)
    if store is None or not isinstance(receipt, Mapping):
        return False
    try:
        state = await state_store_value(store, "load")()
    except Exception:
        return False
    state = sanitize_partial_output_state(state)
    if state is None or not _receipt_matches_state(receipt, state):
        return False
    state["terminalState"] = "committed"
    state["updatedAt"] = now_iso()
    try:
        return bool(await state_store_value(store, "save")(state))
    except Exception:
        return False


def _runtime_authority(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    out = {
        "generation": _positive_integer(value.get("generation")),
        "configRevision": _positive_integer(value.get("configRevision")),
        "runtimeInstanceId": _bounded_string(value.get("runtimeInstanceId"), 128),
        "pathGraphId": _bounded_string(value.get("pathGraphId"), 128),
        "turnAuthorityId": _bounded_string(value.get("turnAuthorityId"), 128),
    }
    return out if any(item is not None for item in out.values()) else None


def _receipt_matches_state(
    receipt: Mapping[str, Any],
    state: Mapping[str, Any],
) -> bool:
    return all(
        receipt.get(key) == state.get(key) for key in ("logicalCallId", "generation")
    ) and all(
        receipt.get(f"{kind}TextHash") == _state_text_hash(state, kind)
        for kind in ("assistant", "reasoning")
    )


def _state_text_hash(state: Mapping[str, Any], kind: str) -> str | None:
    # Text that never streamed leaves no hash in the state, while a receipt
    # always hashes its text: a tool-only call that streamed reasoning commits
    # an empty-text receipt. Unfinalized, it would fail every later call closed.
    value = state.get(f"{kind}TextHash")
    if value is None and not state.get(f"{kind}TextLength"):
        return _EMPTY_TEXT_HASH
    return value


def _bounded_string(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return None
    return normalized if len(normalized) <= maximum else f"{normalized[: maximum - 1]}…"


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return min(value, 9_007_199_254_740_991)


def _nonnegative_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, 9_007_199_254_740_991)


# --- abandoned-output recovery (merged from partial_output_recovery) ---
PARTIAL_OUTPUT_RECOVERY_POLICIES = frozenset(
    {"fail_closed", "discard_if_sink_unavailable"}
)


def assert_partial_output_recovery_policy(value: Any) -> None:
    if value is not None and value not in PARTIAL_OUTPUT_RECOVERY_POLICIES:
        raise ValueError("Partial output recovery policy is invalid.")


def partial_output_recovery_policy(runtime: Mapping[str, Any]) -> str:
    value = runtime.get("partial_output_recovery_policy")
    assert_partial_output_recovery_policy(value)
    return str(value or "fail_closed")


async def recover_abandoned_partial_output(
    *,
    state_store: Any,
    sink: Any,
    recovery_policy: str,
    session_id: str | None,
    logical_call_id: str,
    mode: str,
    runtime_authority: dict[str, Any] | None,
    invoke: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    if state_store is None:
        return _result(True, "no_state_store")
    try:
        existing = await maybe_await(state_store_value(state_store, "load")())
    except Exception:
        return _result(False, "state_load_failed")
    state = sanitize_partial_output_state(
        existing,
        expected_session_id=session_id,
    )
    if state is None or is_terminal_partial_output_state(state):
        return _result(True, "no_abandoned_state")
    discard_without_sink = (
        sink is None
        and (
            not state.get("previewPublished")
            or recovery_policy == "discard_if_sink_unavailable"
        )
    )
    if discard_without_sink:
        state["terminalState"] = "interrupted"
        state["updatedAt"] = now_iso()
        try:
            saved = await maybe_await(state_store_value(state_store, "save")(state))
        except Exception:
            return _result(False, "state_persist_failed")
        reason = (
            "abandoned_preview_discarded"
            if state.get("previewPublished")
            else "abandoned_buffer_discarded"
        )
        return _result(bool(saved), reason)
    if sink is None:
        return _result(False, "recover_unavailable")
    result = await invoke(
        "recover",
        {
            "logicalCallId": logical_call_id,
            "deliveryMode": mode,
            "abandoned": clone_partial_output_state(state),
            "runtimeAuthority": runtime_authority,
            "sameTurnAuthority": _same_turn_authority(
                state.get("runtimeAuthority"), runtime_authority
            ),
            "operationId": (
                f"{logical_call_id}:recover:"
                f"{state['logicalCallId']}:{state['generation']}"
            ),
        },
    )
    if not result["acknowledged"]:
        return result
    state["recoveryAcknowledged"] = True
    state["terminalState"] = "interrupted"
    state["updatedAt"] = now_iso()
    try:
        saved = await maybe_await(state_store_value(state_store, "save")(state))
    except Exception:
        return _result(False, "state_persist_failed")
    return result if saved else _result(False, "state_persist_failed")


def _same_turn_authority(left: Any, right: Any) -> bool:
    return bool(
        isinstance(left, Mapping)
        and isinstance(right, Mapping)
        and left.get("turnAuthorityId")
        and left.get("turnAuthorityId") == right.get("turnAuthorityId")
    )


def _result(acknowledged: bool, reason: str) -> dict[str, Any]:
    return {
        "acknowledged": acknowledged,
        "published": False,
        "reason": reason,
    }
