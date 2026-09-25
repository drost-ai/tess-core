"""The handle a host holds for a session: its id plus the manager. It stays
valid while the session is closed and unloaded, reopens the session on the
next turn, and says so when the session was ended or the runtime stopped."""
from __future__ import annotations

from typing import Any, Callable

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
        return [getattr(tool, "name", None) or tool.get("name") for tool in self._live_session().components["tools"]]

    def subscribe(self, listener: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        """Receive this session's event envelopes, in addition to the runtime listener."""
        live = self._live_session()
        live.subscribers.append(listener)
        return lambda: live.subscribers.remove(listener) if listener in live.subscribers else None

    # -- turns -----------------------------------------------------------

    async def run_turn(
        self,
        prompt: Any,
        *,
        limits: dict[str, int] | None = None,
        turn_id: str | None = None,
        provider_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one user turn after whatever is already queued for this
        session, and return the engine's result. A handle whose session was
        unloaded reopens it first. ``turn_id`` lets the caller name the turn
        before its ``turn.started`` is announced."""
        if self._manager.stopping:
            raise RuntimeStopped("The runtime is stopped; it runs no turn.")
        if self._manager.is_ended(self.id):
            raise SessionEnded(self.id)
        if not self.is_open:
            await self._manager.open(self.id)
        return await self._live_session().turns.run(
            prompt, origin="user", limits=limits, provider_override=provider_override, turn_id=turn_id
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
