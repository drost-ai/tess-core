from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ._utils import maybe_await, now_iso
from .partial_output_state import (
    clonePartialOutputState,
    isTerminalPartialOutputState,
    sanitizePartialOutputState,
    stateStoreValue,
)


PARTIAL_OUTPUT_RECOVERY_POLICIES = frozenset(
    {"fail_closed", "discard_if_sink_unavailable"}
)


def assertPartialOutputRecoveryPolicy(value: Any) -> None:
    if value is not None and value not in PARTIAL_OUTPUT_RECOVERY_POLICIES:
        raise ValueError("Partial output recovery policy is invalid.")


def partialOutputRecoveryPolicy(runtime: Mapping[str, Any]) -> str:
    value = runtime.get("partialOutputRecoveryPolicy")
    assertPartialOutputRecoveryPolicy(value)
    return str(value or "fail_closed")


async def recoverAbandonedPartialOutput(
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
        existing = await maybe_await(stateStoreValue(state_store, "load")())
    except Exception:
        return _result(False, "state_load_failed")
    state = sanitizePartialOutputState(
        existing,
        expected_session_id=session_id,
    )
    if state is None or isTerminalPartialOutputState(state):
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
            saved = await maybe_await(stateStoreValue(state_store, "save")(state))
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
            "abandoned": clonePartialOutputState(state),
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
        saved = await maybe_await(stateStoreValue(state_store, "save")(state))
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
