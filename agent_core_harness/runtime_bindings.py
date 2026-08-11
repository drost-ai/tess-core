from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import agent_core._facade as core


def bind_agent_runtime(
    session: Mapping[str, Any],
    runtime: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> Mapping[str, Any]:
    """Adapt a harness/runtime component surface to the versioned turn ABI."""
    source = dict(runtime or {})
    values = {**source, **overrides}
    gateway = overrides.get("daemon", source.get("daemon"))
    daemon = (
        {"state": "connected", "client": gateway}
        if core.isTaskSupervisorGateway(gateway)
        else {
            "state": "unavailable",
            "reason": values.get("daemonUnavailableReason")
            or {
                "code": "harness.daemon_unavailable",
                "message": "The task supervisor is not connected for this harness turn.",
            },
        }
    )
    get_binding = source.get("getActiveBinding")
    binding = overrides.get("sessionBinding") or (
        get_binding() if callable(get_binding) else source.get("sessionBinding")
    )
    if not isinstance(binding, Mapping):
        binding = core.createRuntimeSessionBinding(session["id"], 1, 1)
    emit = values.get("emitEvent") if callable(values.get("emitEvent")) else lambda *_args: None
    refresh = values.get("refreshMcpToolRegistry")
    if not callable(refresh):
        refresh = lambda: None
    interactive = bool(values.get("interactive", False))
    ask_question = values.get("askQuestion") if callable(values.get("askQuestion")) else lambda *_args: ""
    filtered = {
        key: value
        for key, value in values.items()
        if key in core.AGENT_RUNTIME_BINDING_FIELDS
    }
    return core.createAgentRuntimeBindings(
        {
            **filtered,
            "tools": values.get("tools") or [],
            "emitEvent": emit,
            "daemon": daemon,
            "refreshMcpToolRegistry": refresh,
            "sessionBinding": binding,
            "questionPolicy": {
                "interactive": interactive,
                "askQuestion": ask_question,
            },
        }
    )


__all__ = ["bind_agent_runtime"]
