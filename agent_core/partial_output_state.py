from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from typing import Any

from ._utils import now_iso
from .storage import (
    readJsonFile,
    sessionArchiveDir,
    withRecordTransaction,
    writeJsonFile,
)


PARTIAL_OUTPUT_STATE_STORE_SCHEMA_VERSION = 1
PARTIAL_OUTPUT_STATE_SCHEMA_VERSION = 1
_TERMINAL_STATES = frozenset({"committed", "interrupted", "superseded"})


class PartialOutputStateStore:
    schemaVersion = PARTIAL_OUTPUT_STATE_STORE_SCHEMA_VERSION

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.path = os.path.join(sessionArchiveDir(session_id), "partial-output-state.json")

    async def load(self) -> dict[str, Any] | None:
        value = await readJsonFile(self.path, None)
        return sanitizePartialOutputState(value, expected_session_id=self.session_id)

    async def save(self, value: Mapping[str, Any]) -> bool:
        sanitized = sanitizePartialOutputState(
            value,
            expected_session_id=self.session_id,
        )
        if sanitized is None:
            return False
        async with withRecordTransaction("partial-output", self.session_id):
            await writeJsonFile(self.path, sanitized)
        return True


def createPartialOutputStateStore(session_id: str) -> PartialOutputStateStore:
    return PartialOutputStateStore(session_id)


def assertPartialOutputStateStore(value: Any) -> None:
    if value is None:
        return
    if (
        stateStoreValue(value, "schemaVersion")
        != PARTIAL_OUTPUT_STATE_STORE_SCHEMA_VERSION
        or not callable(stateStoreValue(value, "load"))
        or not callable(stateStoreValue(value, "save"))
    ):
        raise ValueError("Partial output state store is incomplete or incompatible.")


def stateStoreValue(store: Any, name: str) -> Any:
    if isinstance(store, Mapping):
        return store.get(name)
    return getattr(store, name, None)


def partialOutputRuntimeAuthority(value: Any) -> dict[str, Any] | None:
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


def isTerminalPartialOutputState(value: Mapping[str, Any] | None) -> bool:
    return bool(value and value.get("terminalState") in _TERMINAL_STATES)


def sanitizePartialOutputState(
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


def clonePartialOutputState(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


async def finalizePartialOutputState(
    store: Any,
    receipt: Any,
) -> bool:
    assertPartialOutputStateStore(store)
    if store is None or not isinstance(receipt, Mapping):
        return False
    try:
        state = await stateStoreValue(store, "load")()
    except Exception:
        return False
    state = sanitizePartialOutputState(state)
    if state is None or not _receipt_matches_state(receipt, state):
        return False
    state["terminalState"] = "committed"
    state["updatedAt"] = now_iso()
    try:
        return bool(await stateStoreValue(store, "save")(state))
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
        receipt.get(key) == state.get(key)
        for key in (
            "logicalCallId",
            "generation",
            "assistantTextHash",
            "reasoningTextHash",
        )
    )


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
