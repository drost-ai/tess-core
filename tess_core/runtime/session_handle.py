"""The handle a host holds for a session: its id plus the manager. It stays
valid while the session is closed and unloaded, reopens the session on the
next turn, and says so when the session was ended or the runtime stopped."""
from __future__ import annotations

from typing import Any, Callable

from ..config import filter_tools_by_policy
from .session_options import RuntimeStopped, SessionEnded


class Session:
    """A stable handle: the session's id plus the manager."""

    def __init__(self, manager: Any, session_id: str) -> None:
        self._manager = manager
        self.id = session_id

    @property
    def is_open(self) -> bool:
        return self.id in self._manager._live

    def _live_session(self) -> Any:
        live = self._manager._live.get(self.id)
        if live is None:
            raise RuntimeError(f"Session {self.id} is not open.")
        return live

    @property
    def model(self) -> str:
        live = self._live_session()
        return str(live.record.get("model") or live.config.get("model") or "")

    @property
    def permission_mode(self) -> str:
        live = self._live_session()
        return str(live.record.get("permissionMode") or live.config.get("permissionMode") or "")

    @property
    def message_count(self) -> int:
        return len(self._live_session().record.get("messages") or [])

    @property
    def tool_names(self) -> list[str]:
        """The tools a turn of this session is offered: the composed set,
        less whatever the session's tool selection leaves out."""
        live = self._live_session()
        enabled = set(filter_tools_by_policy(live.config))
        names = [getattr(tool, "name", None) or tool.get("name") for tool in live.bindings["tools"]]
        return [name for name in names if name in enabled]

    def subscribe(self, listener: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        """Receive this session's event envelopes, in addition to the runtime listener.
        The subscription lasts until the returned function is called or the session
        ends; it survives the session being unloaded when idle and reopened."""
        listeners = self._manager.subscribers_for(self.id)
        listeners.append(listener)
        return lambda: listeners.remove(listener) if listener in listeners else None

    @property
    def provider_profile(self) -> dict[str, Any]:
        """What the session runs on, read-only: provider, API mode, model,
        effort, the profile's public fields and the system prompt."""
        live = self._live_session()
        config, record = live.config, live.record
        profile = config.get("providerProfile") or {}
        return {
            "provider": config.get("provider"),
            "openaiApiMode": config.get("openaiApiMode"),
            "model": record.get("model") or config.get("model"),
            "effort": record.get("effort") or config.get("effort"),
            "systemPrompt": record.get("systemPrompt") or config.get("systemPrompt") or "",
            "profile": {k: profile.get(k) for k in ("name", "kind", "apiMode", "baseUrl", "defaultModel", "maxOutputTokens", "maxContextLength", "reasoningEffort", "requestOptions")},
        }

    def set_metadata(self, key: str, value: Any) -> None:
        """A host-owned note on the session's record (persisted with the
        next turn): a personal-memory manifest, an origin mark."""
        self._live_session().record.setdefault("metadata", {})[key] = value

    async def mcp_call(self, server: str, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool on one of this session's own MCP servers, through the
        engine's connection: a host needs no MCP client of its own."""
        manager = (self._live_session().components or {}).get("mcp")
        if not manager or not callable(manager.get("callTool")):
            raise RuntimeError(f"Session {self.id} has no MCP connection.")
        return await manager["callTool"](server, tool, arguments or {})

    # -- turns -----------------------------------------------------------

    async def run_turn(
        self,
        prompt: Any,
        *,
        limits: dict[str, int] | None = None,
        turn_id: str | None = None,
        provider_override: dict[str, Any] | None = None,
        tools: list[str] | None = None,
        origin: str = "user",
    ) -> dict[str, Any]:
        """Run one user turn after whatever is already queued for this
        session, and return the engine's result. A handle whose session was
        unloaded reopens it first. ``turn_id`` lets the caller name the turn
        before its ``turn.started`` is announced; ``tools`` narrows this one
        turn to the named tools (``[]`` for none)."""
        if self._manager.stopping:
            raise RuntimeStopped("The runtime is stopped; it runs no turn.")
        if self._manager.is_ended(self.id):
            raise SessionEnded(self.id)
        if not self.is_open:
            await self._manager.open(self.id)
        return await self._live_session().turns.run(
            prompt, origin=origin, limits=limits, provider_override=provider_override, turn_id=turn_id, tool_names=tools
        )

    async def cancel_turn(self, turn_id: str) -> bool:
        """Cancel exactly that turn. True if it was running or queued here."""
        live = self._manager._live.get(self.id)
        return bool(live is not None and live.turns.cancel(turn_id))

    async def end(self, reason: str = "deleted") -> dict[str, Any]:
        """The one explicit instruction: see ``SessionManager.end``."""
        return await self._manager.end(self.id, reason=reason)

    @property
    def busy(self) -> bool:
        live = self._manager._live.get(self.id)
        return bool(live is not None and live.busy())

    def turns(self) -> dict[str, Any]:
        """The running turn and the queue, for a host's status."""
        live = self._manager._live.get(self.id)
        if live is None:
            return {"running": None, "queued": []}
        running = live.turns.running()
        return {"running": running.describe() if running else None, "queued": [turn.describe() for turn in live.turns.queued()]}

    async def close(self) -> None:
        await self._manager.close(self.id)
