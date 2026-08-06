from __future__ import annotations

import time
from typing import Any, Callable

from .._utils import now_iso


PROVIDER_ACTIVITY_KINDS = frozenset(
    {
        "request_preparing",
        "request_started",
        "response_headers",
        "stream_frame",
        "reasoning",
        "output_delta",
        "tool_boundary",
        "retry_wait",
        "terminal",
    }
)

_PHASE_BY_KIND = {
    "request_preparing": "request_preparing",
    "request_started": "request_start",
    "response_headers": "stream_open",
    "stream_frame": "stream_read",
    "reasoning": "stream_read",
    "output_delta": "stream_read",
    "tool_boundary": "stream_read",
    "retry_wait": "retry_wait",
    "terminal": "terminal",
}


class ProviderActivityEmitter:
    """Emit bounded content-free provider activity for one logical call."""

    def __init__(
        self,
        *,
        emitEvent: Callable[[str, dict[str, Any]], Any],
        sessionId: str,
        logicalTurnId: str,
        logicalCallId: str,
        provider: str,
        profile: str,
        model: str,
        coalesceIntervalMs: int = 250,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit_event = emitEvent
        self._session_id = _bounded(sessionId, 128)
        self._logical_turn_id = _bounded(logicalTurnId, 128)
        self._logical_call_id = _bounded(logicalCallId, 128)
        self._provider = _bounded(provider, 64)
        self._profile = _bounded(profile, 128)
        self._model = _bounded(model, 128)
        self._coalesce_seconds = max(0, int(coalesceIntervalMs)) / 1000
        self._monotonic = monotonic
        self._sequence = 0
        self._last_at = float("-inf")
        self._last_high_frequency_at = float("-inf")
        self._last_signature: tuple[str, int] | None = None
        self._terminal = False

    def emit(
        self,
        activityKind: str,
        *,
        attempt: int = 1,
        phase: str | None = None,
        force: bool = False,
    ) -> bool:
        kind = str(activityKind or "").strip()
        if kind not in PROVIDER_ACTIVITY_KINDS:
            return False
        if self._terminal:
            return False
        normalized_attempt = max(1, int(attempt or 1))
        now = self._monotonic()
        signature = (kind, normalized_attempt)
        high_frequency = kind in {
            "stream_frame",
            "reasoning",
            "output_delta",
        }
        repeated = signature == self._last_signature
        if (
            not force
            and (
                (
                    high_frequency
                    and now - self._last_high_frequency_at < self._coalesce_seconds
                )
                or (repeated and now - self._last_at < self._coalesce_seconds)
            )
        ):
            return False
        self._sequence += 1
        self._last_at = now
        if high_frequency:
            self._last_high_frequency_at = now
        self._last_signature = signature
        if kind == "terminal":
            self._terminal = True
        try:
            self._emit_event(
                "provider.activity",
                {
                    "session_id": self._session_id,
                    "logical_turn_id": self._logical_turn_id,
                    "logical_call_id": self._logical_call_id,
                    "attempt": normalized_attempt,
                    "provider": self._provider,
                    "profile": self._profile,
                    "model": self._model,
                    "phase": _bounded(phase or _PHASE_BY_KIND[kind], 32),
                    "activity_kind": kind,
                    "monotonic_sequence": self._sequence,
                    "occurred_at": now_iso(),
                },
            )
        except Exception:
            return False
        return True


def emitProviderActivity(
    runtime: dict[str, Any],
    activityKind: str,
    *,
    attempt: int = 1,
    phase: str | None = None,
    force: bool = False,
) -> bool:
    emitter = runtime.get("providerActivityEmitter")
    if not isinstance(emitter, ProviderActivityEmitter):
        return False
    return emitter.emit(
        activityKind,
        attempt=attempt,
        phase=phase,
        force=force,
    )


def _bounded(value: Any, maximum: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:maximum]


__all__ = [
    "PROVIDER_ACTIVITY_KINDS",
    "ProviderActivityEmitter",
    "emitProviderActivity",
]
