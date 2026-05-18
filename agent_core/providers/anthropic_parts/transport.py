from __future__ import annotations

import time

from .shared import *

async def _race_anthropic_activity(
    awaitable: Any,
    *,
    timeout_ms: int,
    attempt: int = 1,
    phase: str = "request_start",
    hadAssistantOutput: bool = False,
    hadToolUse: bool = False,
    abortSignal: Any = None,
    activityState: dict[str, Any] | None = None,
) -> Any:
    _raise_anthropic_cancelled({"abortSignal": abortSignal}, phase, attempt, hadAssistantOutput, hadToolUse)
    task = asyncio.ensure_future(awaitable)
    timeout_seconds = max(1, timeout_ms) / 1000
    deadline = time.monotonic() + timeout_seconds
    poll_seconds = ANTHROPIC_ABORT_POLL_INTERVAL_MS / 1000
    while True:
        if task.done():
            return task.result()
        if _signal_aborted(abortSignal):
            await _cancel_anthropic_activity_task(task)
            _raise_anthropic_cancelled({"abortSignal": abortSignal}, phase, attempt, hadAssistantOutput, hadToolUse)
        if activityState is not None:
            last_activity = activityState.get("last_activity")
            if isinstance(last_activity, (int, float)):
                deadline = float(last_activity) + timeout_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await _cancel_anthropic_activity_task(task)
            _raise_anthropic_cancelled({"abortSignal": abortSignal}, phase, attempt, hadAssistantOutput, hadToolUse)
            raise _create_anthropic_timeout(
                phase,
                attempt,
                _activity_state_bool(activityState, "had_assistant_output", hadAssistantOutput),
                _activity_state_bool(activityState, "had_tool_use", hadToolUse),
                timeout_ms,
            )
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=min(poll_seconds, remaining))
        except asyncio.TimeoutError:
            if task.done():
                return task.result()
            continue


def _activity_state_bool(activityState: dict[str, Any] | None, key: str, fallback: bool) -> bool:
    if not isinstance(activityState, dict):
        return fallback
    value = activityState.get(key)
    return bool(value) if value is not None else fallback


def _create_anthropic_timeout(phase: str, attempt: int, hadAssistantOutput: bool, hadToolUse: bool, timeout_ms: int) -> Any:
    return createProviderRequestError(
        createProviderFailureRecord(
            provider="anthropic",
            providerLabel="Anthropic",
            phase=phase,
            category="timeout",
            retryable=not hadAssistantOutput and not hadToolUse,
            attempt=attempt,
            hadAssistantOutput=hadAssistantOutput,
            hadToolUse=hadToolUse,
            rawMessage=f"Anthropic request timed out after {timeout_ms}ms while waiting for provider activity.",
        )
    )

async def _cancel_anthropic_activity_task(task: asyncio.Future[Any]) -> None:
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task

def _raise_anthropic_cancelled(runtime: dict[str, Any], phase: str, attempt: int, hadAssistantOutput: bool, hadToolUse: bool) -> None:
    if not _signal_aborted(runtime.get("abortSignal")):
        return
    raise createProviderRequestError(
        createProviderFailureRecord(
            provider="anthropic",
            providerLabel="Anthropic",
            phase=phase,
            category="cancelled",
            retryable=False,
            attempt=attempt,
            hadAssistantOutput=hadAssistantOutput,
            hadToolUse=hadToolUse,
            rawMessage="Anthropic request was cancelled.",
        )
    )

def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))

def _apply_usage_snapshot(target: dict[str, Any], usage: dict[str, Any]) -> None:
    if not isinstance(usage, dict):
        return
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        if usage.get(key) is not None:
            target[key] = usage.get(key)
