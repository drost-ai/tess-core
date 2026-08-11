from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from typing import Any

from .._utils import maybe_await


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
        self._active_task: asyncio.Future[Any] | None = None
        self._active_stream: Any = None
        self._cancelled = False
        self._closed = False

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


def create_provider_turn_session(
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
        from .openai_turn_session import OpenAiProviderTurnSession

        return OpenAiProviderTurnSession(config=config, **values)
    if values["provider"] == "anthropic":
        from .anthropic_turn_session import AnthropicProviderTurnSession

        return AnthropicProviderTurnSession(config=config, **values)
    return StatelessProviderTurnSession(**values)


async def _close_resource(resource: Any) -> None:
    if resource is None:
        return
    closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if not callable(closer):
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await maybe_await(closer())


