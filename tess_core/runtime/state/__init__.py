"""Runtime state store and reducer.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .background import BACKGROUND_RUNTIME_STATE_EVENTS, reduce_background_runtime_state
from .compaction import reduce_compaction_headroom
from .mcp import MCP_RUNTIME_STATE_EVENTS, reduce_mcp_runtime_state
from .reducer import reduce_runtime_state
from .shared import ACTIVE_TASK_STATUSES, summarize_session_record
from .store import (
    create_initial_runtime_state,
    create_runtime_state_store,
    derive_stored_turn_outcome,
)

__all__ = [
    "ACTIVE_TASK_STATUSES",
    "BACKGROUND_RUNTIME_STATE_EVENTS",
    "MCP_RUNTIME_STATE_EVENTS",
    "reduce_background_runtime_state",
    "create_initial_runtime_state",
    "create_runtime_state_store",
    "derive_stored_turn_outcome",
    "reduce_compaction_headroom",
    "reduce_runtime_state",
    "reduce_mcp_runtime_state",
    "summarize_session_record",
]
