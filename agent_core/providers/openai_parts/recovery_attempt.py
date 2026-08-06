from __future__ import annotations

import asyncio
from typing import Any

from ...partial_output import PartialOutputDelivery
from ..activity import emitProviderActivity
from ..errors import (
    ProviderRequestError,
    normalizeUnknownProviderFailure,
)
from .recovery import (
    attach_openai_failure_lineage,
    attach_recovery_failure,
    can_retry_openai_generation,
    finalize_interrupted_openai_preview,
    supersede_openai_generation,
)
from .transport import _emit, _raise_openai_cancelled
from .shared import providerRetryDelayMs


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
        hadToolUse=had_tool_use or had_provisional_tool_call,
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
        _raise_openai_cancelled(
            runtime,
            provider_label,
            phase,
            attempt,
            had_output,
            had_tool_use or had_provisional_tool_call,
        )
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
            runtime,
            provider_label,
            phase,
            attempt,
            had_output,
            had_tool_use or had_provisional_tool_call,
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
