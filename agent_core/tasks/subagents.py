"""Subagent spawn orchestration facade over tasks/subagent_parts."""
from __future__ import annotations

from .subagent_parts.finalizers import (
    finalizeBackgroundSubagentFailureFromEnv,
    finalizeBackgroundSubagentSuccessFromEnv,
)
from .subagent_parts.foreground import runForegroundSubagent
from .subagent_parts.lifecycle import prepareSubagent, startBackgroundSubagent
from .subagent_parts.permissions import (
    inheritParentPermissionMode,
    mapStopReasonToCompletionReason,
    mapStopReasonToStatus,
    mapStopReasonToWaitingReason,
    parseAllowedPrompts,
    sanitizePermissionMode,
)

__all__ = [
    "finalizeBackgroundSubagentFailureFromEnv",
    "finalizeBackgroundSubagentSuccessFromEnv",
    "inheritParentPermissionMode",
    "mapStopReasonToCompletionReason",
    "mapStopReasonToStatus",
    "mapStopReasonToWaitingReason",
    "parseAllowedPrompts",
    "prepareSubagent",
    "runForegroundSubagent",
    "sanitizePermissionMode",
    "startBackgroundSubagent",
]
