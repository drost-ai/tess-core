from __future__ import annotations

from typing import Any

from .turn_session import ProviderTurnSession
from .anthropic_async_transport import AnthropicAsyncHttpConnectionOwner


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
        self._connection_owner: AnthropicAsyncHttpConnectionOwner | None = None
        self._telemetry = {
            "capabilityEnabled": self.keep_alive_enabled,
            "connectionsOpened": 0,
            "requests": 0,
            "reusedRequests": 0,
            "fallbackCount": 0,
            "cancelCloseCount": 0,
        }

    def acquire_anthropic_transport(self) -> AnthropicAsyncHttpConnectionOwner:
        if not self.keep_alive_enabled or self._fallback_latched:
            return AnthropicAsyncHttpConnectionOwner(observe=self._observe_transport)
        if self._connection_owner is None:
            self._connection_owner = AnthropicAsyncHttpConnectionOwner(
                keep_alive=True,
                observe=self._observe_transport,
            )
        return self._connection_owner

    def transport_telemetry(self) -> dict[str, Any]:
        return {**self._telemetry, "fallbackLatched": self._fallback_latched}

    async def cancel(self, reason: str = "cancelled") -> None:
        owner = self._connection_owner
        if owner is not None:
            owner.cancel()
        await super().cancel(reason)

    async def close(self) -> None:
        if self._closed:
            return
        await super().close()
        owner = self._connection_owner
        self._connection_owner = None
        if owner is not None:
            owner.close()

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
