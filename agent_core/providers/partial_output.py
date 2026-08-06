from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from typing import Any

from .._utils import maybe_await, now_iso
from .partial_output_state import (
    partialOutputRecoveryPolicy,
    recoverAbandonedPartialOutput,
)
from .partial_output_state import (
    assertPartialOutputStateStore,
    clonePartialOutputState,
    partialOutputRuntimeAuthority,
    stateStoreValue,
)

PARTIAL_OUTPUT_SINK_SCHEMA_VERSION = 2
PARTIAL_OUTPUT_STATE_STORE_SCHEMA_VERSION = 1
PARTIAL_OUTPUT_ACK_TIMEOUT_MS = 5_000
PARTIAL_OUTPUT_MODES = frozenset({"buffered", "replaceable", "append_only"})
_METHODS = ("begin", "append", "supersede", "reconcile", "commit", "interrupt", "recover")

class PartialOutputDelivery:
    """Generation-fenced adapter for the asynchronous tentative-output sink."""

    def __init__(
        self,
        runtime: Mapping[str, Any],
        logical_call_id: str,
        *,
        session_id: str | None = None,
    ) -> None:
        self.logical_call_id = logical_call_id
        self.session_id = session_id
        self.sink = runtime.get("partialOutputSink")
        self.state_store = runtime.get("partialOutputStateStore")
        assertPartialOutputStateStore(self.state_store)
        self.mode = partialOutputMode(runtime)
        self.recovery_policy = partialOutputRecoveryPolicy(runtime)
        self.timeout_ms = _positive_timeout(
            runtime.get("partialOutputAckTimeoutMs"),
            PARTIAL_OUTPUT_ACK_TIMEOUT_MS,
        )
        self.runtime_authority = partialOutputRuntimeAuthority(runtime)
        self.active_generation: int | None = None
        self._terminal_generation: int | None = None
        self._state: dict[str, Any] | None = None
        self._append_results: dict[tuple[int, str], dict[str, Any]] = {}
        self._supersede_results: dict[tuple[int, int], dict[str, Any]] = {}
        self._commits: set[tuple[int, str]] = set()
        self._interrupts: dict[tuple[int, str], dict[str, Any]] = {}

    async def begin(self, generation: int) -> dict[str, Any]:
        if self._terminal_generation is not None:
            return _result(False, "terminal")
        if generation == self.active_generation:
            return _result(True, "duplicate")
        if self.active_generation is not None:
            return _result(False, "generation_not_superseded")
        if generation == 1:
            recovered = await self._recover_abandoned()
            if not recovered["acknowledged"]:
                return recovered
        self._state = self._new_state(generation)
        if not await self._save_state():
            return _result(False, "state_persist_failed")
        result = await self._invoke(
            "begin",
            {
                "logicalCallId": self.logical_call_id,
                "generation": generation,
                "deliveryMode": self.mode,
                "operationId": f"{self.logical_call_id}:{generation}:begin",
            },
        )
        if result["acknowledged"]:
            self.active_generation = generation
            await self._update_state()
        else:
            await self._update_state(terminalState="interrupted")
        return result

    async def append(
        self,
        generation: int,
        *,
        delta: str,
        text: str,
        response_id: str | None,
        sequence_number: int | None,
        content_kind: str = "assistant",
    ) -> dict[str, Any]:
        if generation != self.active_generation or self._terminal_generation is not None:
            return _result(False, "stale_generation")
        kind = "reasoning" if content_kind == "reasoning" else "assistant"
        text_hash = _text_hash(f"{kind}\0{text}")
        key = (generation, text_hash)
        if key in self._append_results:
            return self._append_results[key]
        result = await self._invoke(
            "append",
            {
                "logicalCallId": self.logical_call_id,
                "generation": generation,
                "deliveryMode": self.mode,
                "delta": delta,
                "text": text,
                "textHash": text_hash,
                "contentKind": kind,
                "responseId": response_id,
                "sequenceNumber": sequence_number,
                "operationId": f"{self.logical_call_id}:{generation}:append:{text_hash}",
            },
        )
        if result["acknowledged"]:
            state_fields = {
                f"{kind}TextHash": _text_hash(text),
                f"{kind}TextLength": len(text),
                "previewPublished": bool(
                    (self._state or {}).get("previewPublished")
                    or result["published"]
                ),
                "responseId": response_id,
                "sequenceNumber": sequence_number,
            }
            if not await self._update_state(**state_fields):
                result = _result(
                    False,
                    "state_persist_failed",
                    published=bool(result["published"]),
                )
        self._append_results[key] = result
        return result

    async def supersede(
        self, old_generation: int, new_generation: int, reason: str
    ) -> dict[str, Any]:
        key = (old_generation, new_generation)
        if key in self._supersede_results:
            return self._supersede_results[key]
        if old_generation != self.active_generation or self._terminal_generation is not None:
            return _result(False, "stale_generation")
        if not await self._update_state(supersessionRequested=True):
            return _result(False, "state_persist_failed")
        result = await self._invoke(
            "supersede",
            {
                "logicalCallId": self.logical_call_id,
                "generation": old_generation,
                "oldGeneration": old_generation,
                "newGeneration": new_generation,
                "deliveryMode": self.mode,
                "reason": str(reason or "")[:256],
                "operationId": (
                    f"{self.logical_call_id}:{old_generation}:supersede:{new_generation}"
                ),
            },
        )
        self._supersede_results[key] = result
        if result["acknowledged"]:
            self.active_generation = None
            saved = await self._update_state(
                supersessionAcknowledged=True,
                terminalState="superseded",
            )
            if not saved:
                result = _result(False, "state_persist_failed")
                self._supersede_results[key] = result
        return result

    async def reconcile(
        self,
        generation: int,
        text: str,
        response_id: str | None,
        *,
        reasoning_text: str = "",
    ) -> dict[str, Any]:
        if generation != self.active_generation or self._terminal_generation is not None:
            return _result(False, "stale_generation")
        text_hash = _text_hash(text)
        result = await self._invoke(
            "reconcile",
            {
                "logicalCallId": self.logical_call_id,
                "generation": generation,
                "deliveryMode": self.mode,
                "text": text,
                "textHash": text_hash,
                "reasoningTextHash": _text_hash(reasoning_text),
                "reasoningTextLength": len(reasoning_text),
                "responseId": response_id,
                "operationId": f"{self.logical_call_id}:{generation}:reconcile:{text_hash}",
            },
        )
        if result["acknowledged"] and not await self._update_state(
            assistantTextHash=text_hash,
            assistantTextLength=len(text),
            reasoningTextHash=_text_hash(reasoning_text),
            reasoningTextLength=len(reasoning_text),
            responseId=response_id,
        ):
            return _result(False, "state_persist_failed")
        return result

    async def commit(
        self,
        generation: int,
        text: str,
        response_id: str | None,
        *,
        reasoning_text: str = "",
    ) -> dict[str, Any]:
        if generation != self.active_generation:
            return _result(False, "stale_generation")
        text_hash = _text_hash(text)
        key = (generation, text_hash)
        if key in self._commits:
            return _result(True, "duplicate")
        result = await self._invoke(
            "commit",
            {
                "logicalCallId": self.logical_call_id,
                "generation": generation,
                "deliveryMode": self.mode,
                "text": text,
                "textHash": text_hash,
                "reasoningTextHash": _text_hash(reasoning_text),
                "reasoningTextLength": len(reasoning_text),
                "responseId": response_id,
                "operationId": f"{self.logical_call_id}:{generation}:commit:{text_hash}",
            },
        )
        if result["acknowledged"]:
            self._commits.add(key)
            self._terminal_generation = generation
            if not await self._update_state(
                assistantTextHash=text_hash,
                assistantTextLength=len(text),
                reasoningTextHash=_text_hash(reasoning_text),
                reasoningTextLength=len(reasoning_text),
                responseId=response_id,
                previewCommitAcknowledged=True,
            ):
                return _result(False, "state_persist_failed")
        return result

    async def interrupt(
        self,
        generation: int,
        text: str,
        failure: Mapping[str, Any],
        *,
        reasoning_text: str = "",
    ) -> dict[str, Any]:
        text_hash = _text_hash(f"{reasoning_text}\0{text}")
        key = (generation, text_hash)
        if key in self._interrupts:
            return self._interrupts[key]
        if generation != self.active_generation or self._terminal_generation is not None:
            return _result(False, "stale_generation")
        result = await self._invoke(
            "interrupt",
            {
                "logicalCallId": self.logical_call_id,
                "generation": generation,
                "deliveryMode": self.mode,
                "text": text,
                "textHash": text_hash,
                "reasoningText": reasoning_text,
                "reasoningTextHash": _text_hash(reasoning_text),
                "failure": {
                    "category": failure.get("category"),
                    "phase": failure.get("phase"),
                    "requestId": failure.get("requestId"),
                },
                "operationId": f"{self.logical_call_id}:{generation}:interrupt:{text_hash}",
            },
        )
        self._interrupts[key] = result
        if result["acknowledged"]:
            self._terminal_generation = generation
            if not await self._update_state(
                assistantTextHash=_text_hash(text),
                assistantTextLength=len(text),
                reasoningTextHash=_text_hash(reasoning_text),
                reasoningTextLength=len(reasoning_text),
                terminalState="interrupted",
            ):
                return _result(False, "state_persist_failed")
        return result

    async def _invoke(self, method: str, operation: dict[str, Any]) -> dict[str, Any]:
        if self.sink is None:
            return _result(
                True,
                "implicit",
                published=method == "append" and self.mode == "append_only",
            )
        function = _sink_value(self.sink, method)
        try:
            value = await asyncio.wait_for(
                maybe_await(function(operation)),
                timeout=self.timeout_ms / 1_000,
            )
        except asyncio.TimeoutError:
            return _result(False, f"{method}_timeout")
        except Exception:
            return _result(False, f"{method}_failed")
        if isinstance(value, Mapping):
            acknowledged = value.get("acknowledged") is True
            published = value.get("published") is True
        else:
            acknowledged = value is True
            published = False
        return {
            "acknowledged": acknowledged,
            "published": published,
            "reason": "acknowledged" if acknowledged else f"{method}_rejected",
        }

    async def _recover_abandoned(self) -> dict[str, Any]:
        return await recoverAbandonedPartialOutput(
            state_store=self.state_store,
            sink=self.sink,
            recovery_policy=self.recovery_policy,
            session_id=self.session_id,
            logical_call_id=self.logical_call_id,
            mode=self.mode,
            runtime_authority=self.runtime_authority,
            invoke=self._invoke,
        )

    def _new_state(self, generation: int) -> dict[str, Any] | None:
        if self.state_store is None or self.session_id is None:
            return None
        timestamp = now_iso()
        return {
            "schemaVersion": 1,
            "sessionId": self.session_id,
            "logicalCallId": self.logical_call_id,
            "generation": generation,
            "deliveryMode": self.mode,
            "assistantTextLength": 0,
            "reasoningTextLength": 0,
            "previewPublished": False,
            "supersessionRequested": False,
            "supersessionAcknowledged": False,
            "recoveryAcknowledged": False,
            "previewCommitAcknowledged": False,
            "terminalState": None,
            "runtimeAuthority": self.runtime_authority,
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }

    async def _update_state(self, **fields: Any) -> bool:
        if self._state is None:
            return True
        self._state.update(fields)
        self._state["updatedAt"] = now_iso()
        return await self._save_state()

    async def _save_state(self) -> bool:
        if self._state is None or self.state_store is None:
            return True
        try:
            return bool(
                await maybe_await(
                    stateStoreValue(self.state_store, "save")(
                        clonePartialOutputState(self._state)
                    )
                )
            )
        except Exception:
            return False

def assertPartialOutputSink(value: Any) -> None:
    if value is None:
        return
    version = _sink_value(value, "schemaVersion")
    mode = _sink_value(value, "mode")
    if (
        version != PARTIAL_OUTPUT_SINK_SCHEMA_VERSION
        or mode not in PARTIAL_OUTPUT_MODES
        or any(not callable(_sink_value(value, name)) for name in _METHODS)
    ):
        raise ValueError("Partial output sink is incomplete or incompatible.")

def partialOutputMode(runtime: Mapping[str, Any]) -> str:
    sink = runtime.get("partialOutputSink")
    if sink is not None:
        assertPartialOutputSink(sink)
        return str(_sink_value(sink, "mode"))
    return "append_only" if runtime.get("includePartialMessages") else "buffered"


def interruptedPreviewReceipt(
    *,
    logical_call_id: str,
    generation: int,
    mode: str,
    text: str,
    reasoning_text: str = "",
    acknowledged: bool,
    reason: str,
) -> dict[str, Any]:
    return {
        "type": "assistant_interrupted_preview",
        "logicalCallId": logical_call_id,
        "generation": generation,
        "deliveryMode": mode,
        "textHash": _text_hash(text),
        "textLength": len(text),
        "reasoningTextHash": _text_hash(reasoning_text),
        "reasoningTextLength": len(reasoning_text),
        "deliveryAcknowledged": acknowledged,
        "reason": str(reason or "")[:256],
    }


def _sink_value(sink: Any, name: str) -> Any:
    if isinstance(sink, Mapping):
        return sink.get(name)
    return getattr(sink, name, None)


def _positive_timeout(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(60_000, max(1, parsed))


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _result(
    acknowledged: bool,
    reason: str,
    *,
    published: bool = False,
) -> dict[str, Any]:
    return {
        "acknowledged": acknowledged,
        "published": published,
        "reason": reason,
    }
