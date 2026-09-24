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
    # A provider send override for every turn of this session, including the
    # background turns the runtime starts itself: the test seam, as
    # ``providerOverride`` is for ``run_agent_prompt``.
    provider_override: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def config_overrides(self, runtime_home: str) -> dict[str, Any]:
        overrides: dict[str, Any] = {"runtimeHome": runtime_home}
        for key, value in (
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
    def __init__(self, session_id: str, unavailable: Unavailable) -> None:
        self.session_id = session_id
        self.reason = unavailable.reason
        self.retry = unavailable.retry
        super().__init__(f"Session {session_id} is unavailable: {unavailable.reason}" + (" (retry later)" if unavailable.retry else ""))


class UnknownSessionError(LookupError):
    """No session with that id exists in the runtime home."""


class SessionBusy(RuntimeError):
    """The session has a turn running or queued; close never cancels."""
