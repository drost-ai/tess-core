"""Subagent policy and durable state: depth, capacity, contracts, launch groups, reconciliation.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .capacity import ACTIVE_TASK_STATUSES, check_subagent_capacity, resolve_subagent_concurrency_policy
from .contract import (
    filter_tools_for_subagent,
    normalize_subagent_runtime_contract,
    runtime_contract_for_agent,
    summarize_subagent_runtime_contract,
)
from .coordination import (
    claimUnconsumedSubagentReconciliations,
    completeSubagentReconciliationClaim,
    listSubagentLaunchGroups,
    listSubagentReconciliations,
    upsertSubagentLaunchGroup,
    withSubagentCoordinationTransaction,
)
from .depth_policy import (
    checkSubagentDepthAllowed,
    requireSubagentDepthAllowed,
    resolveSubagentMaxDepth,
)
from .groups import appendLaunchGroupMetadata, resolveSubagentLaunchGroupId
from .limits import build_subagent_tool_limit_result, check_subagent_runtime_limits, resolve_subagent_runtime_limits
from .reconciliation import (
    TERMINAL_RECONCILIATION_STATUSES,
    appendPendingSubagentReconciliationsToSession,
    buildSubagentReconciliationRecord,
    consumeAppendedSubagentReconciliations,
    createSubagentReconciliationForTerminalChild,
    listUnconsumedSubagentReconciliations,
    markSubagentReconciliationsConsumed,
    readSubagentReconciliations,
    renderSubagentReconciliationMessage,
    subagentReconciliationLogPath,
    upsertSubagentReconciliation,
)
from .result import (
    evaluate_subagent_result,
    extract_structured_result_json,
    persist_structured_subagent_result,
    structured_result_path,
)
from .finalizers import (
    finalizeBackgroundSubagentFailureFromEnv,
    finalizeBackgroundSubagentSuccessFromEnv,
)
from .lifecycle import prepareSubagent, startBackgroundSubagent
from .permissions import (
    inheritParentPermissionMode,
    mapStopReasonToCompletionReason,
    mapStopReasonToStatus,
    mapStopReasonToWaitingReason,
    sanitizePermissionMode,
)

__all__ = [
    "ACTIVE_TASK_STATUSES",
    "TERMINAL_RECONCILIATION_STATUSES",
    "appendLaunchGroupMetadata",
    "appendPendingSubagentReconciliationsToSession",
    "buildSubagentReconciliationRecord",
    "build_subagent_tool_limit_result",
    "checkSubagentDepthAllowed",
    "check_subagent_capacity",
    "check_subagent_runtime_limits",
    "claimUnconsumedSubagentReconciliations",
    "completeSubagentReconciliationClaim",
    "consumeAppendedSubagentReconciliations",
    "createSubagentReconciliationForTerminalChild",
    "evaluate_subagent_result",
    "extract_structured_result_json",
    "filter_tools_for_subagent",
    "finalizeBackgroundSubagentFailureFromEnv",
    "finalizeBackgroundSubagentSuccessFromEnv",
    "inheritParentPermissionMode",
    "listSubagentLaunchGroups",
    "listSubagentReconciliations",
    "listUnconsumedSubagentReconciliations",
    "mapStopReasonToCompletionReason",
    "mapStopReasonToStatus",
    "mapStopReasonToWaitingReason",
    "markSubagentReconciliationsConsumed",
    "normalize_subagent_runtime_contract",
    "persist_structured_subagent_result",
    "prepareSubagent",
    "readSubagentReconciliations",
    "renderSubagentReconciliationMessage",
    "requireSubagentDepthAllowed",
    "resolveSubagentLaunchGroupId",
    "resolveSubagentMaxDepth",
    "resolve_subagent_concurrency_policy",
    "resolve_subagent_runtime_limits",
    "runtime_contract_for_agent",
    "sanitizePermissionMode",
    "startBackgroundSubagent",
    "structured_result_path",
    "subagentReconciliationLogPath",
    "summarize_subagent_runtime_contract",
    "upsertSubagentLaunchGroup",
    "upsertSubagentReconciliation",
    "withSubagentCoordinationTransaction",
]
