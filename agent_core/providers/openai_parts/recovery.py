from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from ...partial_output import PartialOutputDelivery, interruptedPreviewReceipt
from ...provider_activity import emitProviderActivity
from ...provider_errors import (
    ProviderRequestError,
    createProviderFailureRecord,
    createProviderRequestError,
    normalizeUnknownProviderFailure,
    providerRetryDelayMs,
)
from .normalization import _extract_output_blocks
from .transport import _emit, _raise_openai_cancelled


def create_openai_logical_call_id() -> str:
    return f"openai-{uuid.uuid4().hex}"


def attach_openai_failure_lineage(
    failure: ProviderRequestError,
    *,
    mode: str,
    logical_call_id: str,
    generation: int,
    delivery_mode: str,
    response_id: str | None,
    sequence_number: int | None,
) -> ProviderRequestError:
    if mode != "codex":
        return failure
    failure.record.update(
        {
            "logicalCallId": logical_call_id,
            "generation": generation,
            "partialOutputMode": delivery_mode,
            "responseId": _bounded_identifier(response_id),
            "sequenceNumber": _sequence_number(sequence_number),
        }
    )
    return failure


def can_retry_openai_generation(
    failure: ProviderRequestError,
    *,
    mode: str,
    max_attempts: int,
    response_committed: bool,
    tool_effect_committed: bool,
    delivery: PartialOutputDelivery | None,
    had_tentative_output: bool,
    had_provisional_tool_call: bool,
) -> bool:
    record = failure.record
    if (
        not record.get("retryable")
        or int(record.get("attempt") or 0) >= max_attempts
        or response_committed
        or tool_effect_committed
    ):
        return False
    if not had_tentative_output and not had_provisional_tool_call:
        return True
    if mode != "codex":
        return False
    if had_provisional_tool_call and not had_tentative_output:
        return True
    if delivery is None:
        return False
    return delivery.mode in {"buffered", "replaceable"}


async def supersede_openai_generation(
    delivery: PartialOutputDelivery | None,
    *,
    old_generation: int,
    new_generation: int,
    failure: ProviderRequestError,
) -> dict[str, Any]:
    if delivery is None:
        return {"acknowledged": True, "reason": "output_free"}
    return await delivery.supersede(
        old_generation,
        new_generation,
        str(failure.record.get("message") or "retryable provider failure"),
    )


def sink_failure(
    *,
    provider_label: str,
    phase: str,
    attempt: int,
    had_output: bool,
    had_tool_use: bool,
    logical_call_id: str,
    delivery_mode: str,
    reason: str,
    response_id: str | None = None,
) -> ProviderRequestError:
    return createProviderRequestError(
        createProviderFailureRecord(
            provider="openai",
            providerLabel=provider_label,
            phase=phase,
            category="protocol",
            retryable=False,
            attempt=attempt,
            hadAssistantOutput=had_output,
            hadToolUse=had_tool_use,
            rawMessage=f"Partial output sink rejected {reason}.",
            logicalCallId=logical_call_id,
            generation=attempt,
            partialOutputMode=delivery_mode,
            responseId=_bounded_identifier(response_id),
            recoveryFailure=reason,
        )
    )


def attach_recovery_failure(
    failure: ProviderRequestError, reason: str
) -> ProviderRequestError:
    failure.record["recoveryFailure"] = str(reason or "")[:128] or None
    return failure


def attach_interrupted_preview(
    failure: ProviderRequestError,
    *,
    logical_call_id: str,
    generation: int,
    delivery_mode: str,
    text: str,
    reasoning_text: str,
    acknowledged: bool,
    reason: str,
) -> ProviderRequestError:
    failure.record["interruptedPreview"] = interruptedPreviewReceipt(
        logical_call_id=logical_call_id,
        generation=generation,
        mode=delivery_mode,
        text=text,
        reasoning_text=reasoning_text,
        acknowledged=acknowledged,
        reason=reason,
    )
    return failure


async def finalize_interrupted_openai_preview(
    failure: ProviderRequestError,
    *,
    delivery: PartialOutputDelivery | None,
    runtime: dict[str, Any],
    emitted_text: str,
    reasoning_text: str,
    preview_published: bool,
    logical_call_id: str,
    generation: int,
    response_id: str | None,
    sequence_number: int | None,
    model: str,
    emit: Any,
) -> ProviderRequestError:
    interrupted = (
        await delivery.interrupt(
            generation,
            emitted_text,
            failure.record,
            reasoning_text=reasoning_text,
        )
        if delivery is not None
        else {"acknowledged": False, "reason": "sink_unavailable"}
    )
    acknowledged = bool(interrupted["acknowledged"])
    if (delivery is None or delivery.sink is None) and runtime.get(
        "includePartialMessages"
    ):
        marker = "\n\n[Response interrupted before completion.]"
        event = "message.assistant.partial"
        payload = {
            "session_id": None,
            "model": model,
            "text_delta": marker,
            "text": f"{emitted_text}{marker}",
            "logicalCallId": logical_call_id,
            "generation": generation,
            "responseId": response_id,
            "sequenceNumber": sequence_number,
            "deliveryMode": "append_only",
        }
        if not emitted_text and reasoning_text:
            event = "message.assistant.thinking.partial"
            payload = {
                **payload,
                "thinking_delta": marker,
                "thinking": f"{reasoning_text}{marker}",
                "reasoning_kind": "unknown", "user_visible": False,
                "provider_event_type": "provider.interrupted_reasoning",
            }
        emit(event, payload)
        acknowledged = True
        preview_published = True
    if preview_published:
        attach_interrupted_preview(
            failure,
            logical_call_id=logical_call_id,
            generation=generation,
            delivery_mode=delivery.mode if delivery else "append_only",
            text=emitted_text,
            reasoning_text=reasoning_text,
            acknowledged=acknowledged,
            reason=str(interrupted["reason"]),
        )
    return failure


def authoritative_openai_response(
    output: list[Any],
) -> tuple[list[dict[str, Any]], str, bool, bool, str]:
    blocks = _extract_output_blocks(output)
    text = "".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    )
    had_tool_use = any(block.get("type") == "tool_use" for block in blocks)
    return (
        blocks,
        text,
        bool(text),
        had_tool_use,
        "tool_calls" if had_tool_use else "stop",
    )


async def append_openai_partial(
    delivery: PartialOutputDelivery | None,
    *,
    generation: int,
    content_kind: str,
    delta: str,
    text: str,
    response_id: str | None,
    sequence_number: int | None,
    provider_label: str,
    phase: str,
    had_output: bool,
    had_tool_use: bool,
    logical_call_id: str,
) -> dict[str, Any]:
    if delivery is None:
        return {"acknowledged": True, "published": False}
    appended = await delivery.append(
        generation,
        delta=delta,
        text=text,
        response_id=response_id,
        sequence_number=sequence_number,
        content_kind=content_kind,
    )
    if not appended["acknowledged"]:
        raise sink_failure(
            provider_label=provider_label,
            phase=phase,
            attempt=generation,
            had_output=had_output,
            had_tool_use=had_tool_use,
            logical_call_id=logical_call_id,
            delivery_mode=delivery.mode,
            reason=str(appended["reason"]),
            response_id=response_id,
        )
    return appended


async def commit_openai_delivery(
    delivery: PartialOutputDelivery | None,
    *,
    generation: int,
    authoritative_text: str,
    reasoning_text: str,
    response_id: str | None,
    provider_label: str,
    phase: str,
    had_output: bool,
    had_tool_use: bool,
    logical_call_id: str,
) -> dict[str, Any] | None:
    if delivery is None:
        return None
    common = {
        "provider_label": provider_label,
        "phase": phase,
        "attempt": generation,
        "had_output": had_output,
        "had_tool_use": had_tool_use,
        "logical_call_id": logical_call_id,
        "delivery_mode": delivery.mode,
        "response_id": response_id,
    }
    reconciled = await delivery.reconcile(
        generation,
        authoritative_text,
        response_id,
        reasoning_text=reasoning_text,
    )
    if not reconciled["acknowledged"]:
        raise sink_failure(reason=str(reconciled["reason"]), **common)
    committed = await delivery.commit(
        generation,
        authoritative_text,
        response_id,
        reasoning_text=reasoning_text,
    )
    if not committed["acknowledged"]:
        raise sink_failure(reason=str(committed["reason"]), **common)
    if delivery.state_store is None:
        return None
    return {
        "logicalCallId": logical_call_id,
        "generation": generation,
        "assistantTextHash": _recovery_text_hash(authoritative_text),
        "reasoningTextHash": _recovery_text_hash(reasoning_text),
        "responseId": response_id,
    }


def partial_openai_event_lineage(
    *,
    mode: str,
    delivery: PartialOutputDelivery | None,
    logical_call_id: str,
    generation: int,
    response_id: str | None,
    sequence_number: int | None,
) -> dict[str, Any]:
    if mode != "codex":
        return {}
    return {
        "logicalCallId": logical_call_id,
        "generation": generation,
        "responseId": response_id,
        "sequenceNumber": sequence_number,
        "deliveryMode": delivery.mode if delivery else "append_only",
    }


def _recovery_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def recover_openai_attempt(
    *,
    error: Exception,
    args: dict[str, Any],
    mode: str,
    provider_label: str,
    phase: str,
    attempt: int,
    max_attempts: int,
    had_output: bool,
    had_tool_use: bool,
    emitted_text: str,
    reasoning_text: str,
    had_provisional_tool_call: bool,
    response_committed: bool,
    delivery: PartialOutputDelivery | None,
    preview_published: bool,
    logical_call_id: str,
    response_id: str | None,
    sequence_number: int | None,
    retry_delay: Any,
) -> tuple[ProviderRequestError, bool]:
    runtime = args.get("runtime") or {}
    failure = normalizeUnknownProviderFailure(
        error=error,
        provider="openai",
        providerLabel=provider_label,
        phase=phase,
        attempt=attempt,
        hadAssistantOutput=had_output,
        hadToolUse=had_tool_use,
    )
    attach_openai_failure_lineage(
        failure,
        mode=mode,
        logical_call_id=logical_call_id,
        generation=attempt,
        delivery_mode=delivery.mode if delivery else (
            "append_only" if runtime.get("includePartialMessages") else "buffered"
        ),
        response_id=response_id,
        sequence_number=sequence_number,
    )
    can_retry = can_retry_openai_generation(
        failure,
        mode=mode,
        max_attempts=max_attempts,
        response_committed=response_committed,
        tool_effect_committed=bool(runtime.get("toolEffectCommitted")),
        delivery=delivery,
        had_tentative_output=bool(emitted_text or reasoning_text),
        had_provisional_tool_call=had_provisional_tool_call,
    )
    has_tentative_state = bool(
        emitted_text or reasoning_text or had_provisional_tool_call
    )
    if can_retry:
        if has_tentative_state:
            _emit(
                runtime,
                "provider.attempt_superseding",
                {**failure.record, "next_generation": attempt + 1},
            )
        superseded = await supersede_openai_generation(
            delivery,
            old_generation=attempt,
            new_generation=attempt + 1,
            failure=failure,
        )
        if not superseded["acknowledged"]:
            attach_recovery_failure(failure, str(superseded["reason"]))
            can_retry = False
        elif has_tentative_state:
            _emit(
                runtime,
                "provider.attempt_superseded",
                {**failure.record, "next_generation": attempt + 1},
            )
    if can_retry:
        delay_ms = openai_retry_delay_ms(args, attempt, retry_delay)
        _raise_openai_cancelled(runtime, provider_label, phase, attempt, had_output, had_tool_use)
        _emit(
            runtime,
            "provider.retrying",
            {
                **failure.record,
                "next_attempt": attempt + 1,
                **({"next_generation": attempt + 1} if mode == "codex" else {}),
                "delay_ms": delay_ms,
            },
        )
        emitProviderActivity(
            runtime,
            "retry_wait",
            attempt=attempt,
            force=True,
        )
        await asyncio.sleep(delay_ms / 1_000)
        _raise_openai_cancelled(
            runtime, provider_label, phase, attempt, had_output, had_tool_use
        )
        return failure, True
    if (emitted_text or reasoning_text) and mode == "codex":
        await finalize_interrupted_openai_preview(
            failure,
            delivery=delivery,
            runtime=runtime,
            emitted_text=emitted_text,
            reasoning_text=reasoning_text,
            preview_published=preview_published,
            logical_call_id=logical_call_id,
            generation=attempt,
            response_id=response_id,
            sequence_number=sequence_number,
            model=args["request"]["model"],
            emit=lambda event, data: _emit(runtime, event, data),
        )
    return failure, False


def openai_retry_delay_ms(
    args: dict[str, Any], attempt: int, default_delay: Any = providerRetryDelayMs
) -> int:
    override = args.get("retryDelay")
    if callable(override):
        try:
            return max(0, int(override(attempt)))
        except (TypeError, ValueError):
            pass
    return int(default_delay(attempt))


def _bounded_identifier(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.strip()
    return stripped if len(stripped) <= 256 else f"{stripped[:255]}…"


def _sequence_number(value: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, 9_007_199_254_740_991)
