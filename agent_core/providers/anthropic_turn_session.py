from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any

from ..provider_turn_session import ProviderTurnSession
from .anthropic_http_transport import AnthropicHttpConnectionOwner


class AnthropicProviderTurnSession(ProviderTurnSession):
    def __init__(self, *, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        profile_capabilities = (config.get("providerProfile") or {}).get("capabilities") or {}
        direct_capabilities = config.get("providerCapabilities") or {}
        capabilities = {
            **(profile_capabilities if isinstance(profile_capabilities, dict) else {}),
            **(direct_capabilities if isinstance(direct_capabilities, dict) else {}),
        }
        self.keep_alive_enabled = capabilities.get("anthropicHttpKeepAlive") is True
        self._fallback_latched = False
        self._connection_owner: AnthropicHttpConnectionOwner | None = None
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()
        self._telemetry = {
            "capabilityEnabled": self.keep_alive_enabled,
            "connectionsOpened": 0,
            "requests": 0,
            "reusedRequests": 0,
            "fallbackCount": 0,
            "cancelCloseCount": 0,
            "threadsStarted": 0,
            "threadsSettled": 0,
            "threadLeaks": 0,
        }

    def acquire_anthropic_transport(self) -> AnthropicHttpConnectionOwner:
        if not self.keep_alive_enabled or self._fallback_latched:
            return AnthropicHttpConnectionOwner(observe=self._observe_transport)
        if self._connection_owner is None:
            self._connection_owner = AnthropicHttpConnectionOwner(
                keep_alive=True,
                observe=self._observe_transport,
            )
        return self._connection_owner

    def register_transport_worker(self, thread: threading.Thread) -> None:
        with self._workers_lock:
            self._workers.add(thread)
            self._telemetry["threadsStarted"] += 1

    def unregister_transport_worker(self, thread: threading.Thread) -> None:
        with self._workers_lock:
            if thread in self._workers:
                self._workers.remove(thread)
                self._telemetry["threadsSettled"] += 1

    def transport_telemetry(self) -> dict[str, Any]:
        return {**self._telemetry, "fallbackLatched": self._fallback_latched}

    async def cancel(self, reason: str = "cancelled") -> None:
        owner = self._connection_owner
        if owner is not None:
            owner.cancel()
        await super().cancel(reason)
        await self._settle_workers()

    async def close(self) -> None:
        if self._closed:
            return
        await super().close()
        owner = self._connection_owner
        self._connection_owner = None
        if owner is not None:
            owner.close()
        await self._settle_workers()

    async def _settle_workers(self, timeout: float = 0.25) -> None:
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            if worker is threading.current_thread():
                continue
            await asyncio.to_thread(worker.join, timeout)
            if worker.is_alive():
                self._telemetry["threadLeaks"] += 1
                self._emit_thread_leak(worker, timeout)

    def _observe_transport(self, event: str, fields: dict[str, Any]) -> None:
        if event == "connection_opened":
            self._telemetry["connectionsOpened"] += 1
        elif event == "request_started":
            self._telemetry["requests"] += 1
            if fields.get("reused"):
                self._telemetry["reusedRequests"] += 1
        elif event == "transport_failed" and not self._fallback_latched:
            self._fallback_latched = True
            self._telemetry["fallbackCount"] += 1
            owner = self._connection_owner
            self._connection_owner = None
            if owner is not None:
                owner.close()
        elif event == "cancel_closed":
            self._telemetry["cancelCloseCount"] += 1

    def _emit_thread_leak(self, worker: threading.Thread, timeout: float) -> None:
        with contextlib.suppress(Exception):
            self.runtime.get("emitEvent", lambda *_: None)(
                "provider.transport_thread_leak",
                {
                    "provider": "anthropic",
                    "thread_name": worker.name,
                    "join_timeout_ms": int(timeout * 1_000),
                    "logical_turn_id": self.logical_turn_id,
                    "session_id": self.session_id,
                },
            )
