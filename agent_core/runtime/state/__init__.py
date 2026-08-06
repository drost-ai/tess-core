"""Runtime state store and reducer.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .background import BACKGROUND_RUNTIME_STATE_EVENTS, reduce_background_runtime_state
from .compaction import reduceCompactionHeadroom
from .mcp import MCP_RUNTIME_STATE_EVENTS, reduce_mcp_runtime_state
from .reducer import reduceRuntimeState
from .shared import ACTIVE_TASK_STATUSES, summarizeSessionRecord
from .store import (
    createInitialRuntimeState,
    createRuntimeStateStore,
    deriveStoredTurnOutcome,
)

__all__ = [
    "ACTIVE_TASK_STATUSES",
    "BACKGROUND_RUNTIME_STATE_EVENTS",
    "MCP_RUNTIME_STATE_EVENTS",
    "reduce_background_runtime_state",
    "createInitialRuntimeState",
    "createRuntimeStateStore",
    "deriveStoredTurnOutcome",
    "reduceCompactionHeadroom",
    "reduceRuntimeState",
    "reduce_mcp_runtime_state",
    "summarizeSessionRecord",
]
