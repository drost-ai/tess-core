"""What a host answers when the runtime asks how a session is set up, and
the errors the session manager raises.

``session_options(session_id, reason)`` returns :class:`SessionOptions` or
:class:`Unavailable`. The manager asks for a new session too, with the id it
is about to create, so the host can key options by id from the start.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from ..tools.definitions import ToolDefinition
from .execution_budget import ExecutionBudget
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class SessionOptions:
    """How one session is set up. Everything is optional; the runtime home
    is the runtime's and the workspace defaults to the runtime's cwd."""

    cwd: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    add_dirs: tuple[str, ...] = ()
    mcp_servers: tuple[dict[str, Any], ...] = ()
    settings: str | None = None
    strict_mcp_config: bool = False
    system_prompt: str | None = None
    context_provider: Callable[[str], Awaitable[str | None] | str | None] | None = None
    # The owner's IANA time zone (America/Los_Angeles): the zone the agent's
    # scheduler tools use unless told otherwise.
    timezone: str | None = None
    # A provider send override for every turn of this session, including the
    # background turns the runtime starts itself: the test seam, as
    # ``providerOverride`` is for ``run_agent_prompt``.
    provider_override: dict[str, Any] | None = None
    # The host's last word on the resolved config (a pinned credential, a
    # request option the profile cannot express); applied once at open.
    adjust_config: Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]] | None = None
    # Raw provider stream events for the host (usage extraction); off by default.
    stream_provider_messages: bool = False
    host_tools: tuple[ToolDefinition, ...] = ()
    execution_budget: ExecutionBudget | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    provider_registry: Any = None
    provider_auth_path: str | None = None
    # An existing session keeps the provider profile and model it is bound to,
    # whatever ``provider`` and ``model`` say here: a change of the host's
    # default never moves a conversation. True makes this open's provider,
    # model and effort the session's from now on, for a host whose owner
    # chose another model for this one session. It is read when the session
    # is opened, so close an open session to apply it.
    rebind_provider: bool = False

    def rebind(self, record: dict[str, Any], config: dict[str, Any]) -> bool:
        """With ``rebind_provider``, make an existing session's record follow
        the configuration just resolved for it, so its turns resolve the new
        profile and model. True when anything changed."""
        bound = {key: config.get(key) for key in ("provider", "providerProfileName", "model", "effort")}
        if not self.rebind_provider or all(record.get(key) == value for key, value in bound.items()):
            return False
        record.update(bound)
        return True

    def config_overrides(self, runtime_home: str) -> dict[str, Any]:
        overrides: dict[str, Any] = {"runtimeHome": runtime_home}
        for key, value in (
            ("providerRegistry", self.provider_registry),
            ("providerAuthPath", self.provider_auth_path),
            ("provider", self.provider),
            ("model", self.model),
            ("effort", self.effort),
            ("permissionMode", self.permission_mode),
            ("settings", self.settings),
            ("systemPrompt", self.system_prompt),
        ):
            if value is not None:
                overrides[key] = value
        if self.add_dirs:
            overrides["addDir"] = [os.path.abspath(os.path.expanduser(item)) for item in self.add_dirs]
        if self.mcp_servers:
            overrides["mcpConfig"] = [json.dumps(server) for server in self.mcp_servers]
        if self.strict_mcp_config:
            overrides["strictMcpConfig"] = True
        overrides.update(self.extra)
        return overrides


@dataclass(frozen=True)
class Unavailable:
    """The host refuses or defers opening this session."""

    reason: str
    retry: bool = False


class SessionUnavailableError(RuntimeError):
    """Raised when the host's ``session_options`` answered ``Unavailable`` for a session.

    Carries the ``reason`` and whether to ``retry``.
    """
    def __init__(self, session_id: str, unavailable: Unavailable) -> None:
        self.session_id = session_id
        self.reason = unavailable.reason
        self.retry = unavailable.retry
        super().__init__(f"Session {session_id} is unavailable: {unavailable.reason}" + (" (retry later)" if unavailable.retry else ""))


class UnknownSessionError(LookupError):
    """No session with that id exists in the runtime home."""


class SessionBusy(RuntimeError):
    """The session has a turn running or queued; close never cancels."""


class RuntimeStopped(RuntimeError):
    """The runtime is stopping or stopped: it takes no new sessions or turns."""


class SessionEnded(RuntimeError):
    """The session was ended, the one explicit instruction: it never reopens
    and takes no more work."""

    def __init__(self, session_id: str, reason: str | None = None, at: str | None = None) -> None:
        self.session_id = session_id
        self.reason = reason
        self.at = at
        super().__init__(f"Session {session_id} was ended" + (f" ({reason})" if reason else "") + (f" at {at}" if at else "") + ".")
