"""Remote plane: remote-session records and the durable bridge transport.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .bridge_admission import admit_bridge_message, send_bridge_message
from .bridge_diagnostics import format_bridge_tail, get_bridge_diagnostics, list_active_bridge_rows
from .bridge_generation import close_current_bridge_generation, is_current_bridge_generation
from .bridge_recovery import (
    BridgeRecoveryBlockedError,
    activate_bridge_generation,
    inspect_bridge_recovery,
    resolve_bridge_recovery,
)
from .bridge_runtime import (
    BridgeRuntimeController,
    LifecycleClosedError,
    create_bridge_runtime,
    create_bridge_runtime_controller,
)
from .sessions import (
    create_remote_session,
    find_matching_remote_sessions,
    format_remote_session_candidate,
    update_remote_session,
)

__all__ = [
    "BridgeRecoveryBlockedError",
    "BridgeRuntimeController",
    "LifecycleClosedError",
    "activate_bridge_generation",
    "admit_bridge_message",
    "close_current_bridge_generation",
    "create_bridge_runtime",
    "create_bridge_runtime_controller",
    "create_remote_session",
    "find_matching_remote_sessions",
    "format_bridge_tail",
    "format_remote_session_candidate",
    "get_bridge_diagnostics",
    "inspect_bridge_recovery",
    "is_current_bridge_generation",
    "list_active_bridge_rows",
    "resolve_bridge_recovery",
    "send_bridge_message",
    "update_remote_session",
]
