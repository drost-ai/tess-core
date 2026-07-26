from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from typing import Any

from ._utils import maybe_await


class ProviderTurnSession:
    """Turn-owned provider lifecycle and activity state.

    Provider state held here is deliberately ephemeral.  A fresh instance is
    created for every logical user turn and none of its transport state is
    written into the canonical Agent Core session.
    """

    def __init__(
        self,
        *,
        logical_turn_id: str,
        session_id: str,
        provider: str,
        profile: str | None,
        model: str,
        runtime: dict[str, Any],
    ) -> None:
        self.logical_turn_id = logical_turn_id
        self.session_id = session_id
        self.provider = provider
        self.profile = profile
        self.model = model
        self.runtime = runtime
        self._logical_call_count = 0
        self._activity_sequence = 0
        self._last_coalesced_activity_at: dict[str, float] = {}
        self._active_task: asyncio.Future[Any] | None = None
        self._active_stream: Any = None
        self._cancelled = False
        self._closed = False

    def start_logical_call(self, model: str | None = None) -> str:
        self._logical_call_count += 1
        if model:
            self.model = model
        return f"{self.logical_turn_id}:provider-{self._logical_call_count}"

    async def prepare(self, request: Any) -> Any:
        return request

    async def record_success(self, request: Any, response: Any) -> None:
        return None

    async def record_failure(self, request: Any, failure: BaseException) -> None:
        return None

    def bind_activity_task(self, task: asyncio.Future[Any]) -> None:
        self._active_task = task

    def clear_activity_task(self, task: asyncio.Future[Any]) -> None:
        if self._active_task is task:
            self._active_task = None

    def bind_stream(self, stream: Any) -> None:
        self._active_stream = stream

    async def release_stream(self, stream: Any, *, close: bool = True) -> None:
        if self._active_stream is stream:
            self._active_stream = None
        if close:
            await _close_resource(stream)

    def emit_activity(
        self,
        *,
        logical_call_id: str,
        attempt: int,
        phase: str,
        activity_kind: str,
        model: str | None = None,
    ) -> None:
        if activity_kind in {"stream_frame", "reasoning", "output_delta"}:
            now = time.monotonic()
            previous = self._last_coalesced_activity_at.get(activity_kind)
            if previous is not None and now - previous < 0.25:
                return
            self._last_coalesced_activity_at[activity_kind] = now
        self._activity_sequence += 1
        payload = {
            "session_id": self.session_id,
            "logical_turn_id": self.logical_turn_id,
            "logical_call_id": logical_call_id,
            "attempt": max(1, int(attempt or 1)),
            "provider": self.provider,
            "profile": self.profile,
            "model": model or self.model,
            "phase": phase,
            "activity_kind": activity_kind,
            "monotonic_sequence": self._activity_sequence,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.runtime.get("emitEvent", lambda *_: None)("provider.activity", payload)
        except Exception:
            pass

    async def cancel(self, reason: str = "cancelled") -> None:
        if self._cancelled:
            return
        self._cancelled = True
        task = self._active_task
        self._active_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        stream = self._active_stream
        self._active_stream = None
        await _close_resource(stream)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.cancel("turn_closed")


class StatelessProviderTurnSession(ProviderTurnSession):
    pass


def createProviderTurnSession(
    *,
    logical_turn_id: str,
    session: dict[str, Any],
    config: dict[str, Any],
    runtime: dict[str, Any],
    model: str,
) -> ProviderTurnSession:
    values = {
        "logical_turn_id": logical_turn_id,
        "session_id": str(session.get("id") or ""),
        "provider": str(config.get("provider") or session.get("provider") or ""),
        "profile": session.get("providerProfileName") or config.get("providerProfileName"),
        "model": model,
        "runtime": runtime,
    }
    if values["provider"] == "openai":
        from .providers.openai_turn_session import OpenAiProviderTurnSession

        return OpenAiProviderTurnSession(config=config, **values)
    return StatelessProviderTurnSession(**values)


async def _close_resource(resource: Any) -> None:
    if resource is None:
        return
    closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if not callable(closer):
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await maybe_await(closer())


create_provider_turn_session = createProviderTurnSession
