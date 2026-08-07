"""Subagent policy and durable state: depth, capacity, contracts, launch groups, reconciliation.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .capacity import ACTIVE_TASK_STATUSES, check_subagent_capacity, resolve_subagent_concurrency_policy
from .continuation import (
    defaultParentContinuationPrompt,
    evaluateParentContinuationReadiness,
)
from .continuation_recovery import LEGACY_CLAIM_GRACE_MS, TERMINAL_TASK_STATUSES, recoverSubagentLaunchGroupContinuations
from .contract import (
    filter_tools_for_subagent,
    normalize_subagent_runtime_contract,
    runtime_contract_for_agent,
    summarize_subagent_runtime_contract,
)
from .coordination import (
    DEFAULT_CONTINUATION_LEASE_MS,
    claimSubagentLaunchGroupContinuation,
    claimUnconsumedSubagentReconciliations,
    completeSubagentReconciliationClaim,
    listSubagentLaunchGroups,
    listSubagentReconciliations,
    markSubagentLaunchGroupContinuationCompleted,
    markSubagentLaunchGroupContinuationRunning,
    markSubagentLaunchGroupContinuationStarting,
    releaseSubagentLaunchGroupContinuationClaim,
    upsertSubagentLaunchGroup,
    withSubagentCoordinationTransaction,
)
from .depth_policy import (
    DEFAULT_SUBAGENT_MAX_DEPTH,
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
from .result_payload import (
    boundedSubagentError,
    boundedSubagentEventSummary,
    boundedSubagentRuntimeContract,
    shapeForegroundSubagentResult,
)
from ..tasks.subagents import (
    finalizeBackgroundSubagentFailureFromEnv,
    finalizeBackgroundSubagentSuccessFromEnv,
    inheritParentPermissionMode,
    mapStopReasonToCompletionReason,
    mapStopReasonToStatus,
    mapStopReasonToWaitingReason,
    parseAllowedPrompts,
    prepareSubagent,
    sanitizePermissionMode,
    startBackgroundSubagent,
)

__all__ = [
    "ACTIVE_TASK_STATUSES",
    "DEFAULT_CONTINUATION_LEASE_MS",
    "DEFAULT_SUBAGENT_MAX_DEPTH",
    "LEGACY_CLAIM_GRACE_MS",
    "TERMINAL_RECONCILIATION_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "appendLaunchGroupMetadata",
    "appendPendingSubagentReconciliationsToSession",
    "boundedSubagentError",
    "boundedSubagentEventSummary",
    "boundedSubagentRuntimeContract",
    "buildSubagentReconciliationRecord",
    "build_subagent_tool_limit_result",
    "checkSubagentDepthAllowed",
    "check_subagent_capacity",
    "check_subagent_runtime_limits",
    "claimSubagentLaunchGroupContinuation",
    "claimUnconsumedSubagentReconciliations",
    "completeSubagentReconciliationClaim",
    "consumeAppendedSubagentReconciliations",
    "createSubagentReconciliationForTerminalChild",
    "defaultParentContinuationPrompt",
    "evaluateParentContinuationReadiness",
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
    "markSubagentLaunchGroupContinuationCompleted",
    "markSubagentLaunchGroupContinuationRunning",
    "markSubagentLaunchGroupContinuationStarting",
    "markSubagentReconciliationsConsumed",
    "normalize_subagent_runtime_contract",
    "parseAllowedPrompts",
    "persist_structured_subagent_result",
    "prepareSubagent",
    "readSubagentReconciliations",
    "recoverSubagentLaunchGroupContinuations",
    "releaseSubagentLaunchGroupContinuationClaim",
    "renderSubagentReconciliationMessage",
    "requireSubagentDepthAllowed",
    "resolveSubagentLaunchGroupId",
    "resolveSubagentMaxDepth",
    "resolve_subagent_concurrency_policy",
    "resolve_subagent_runtime_limits",
    "runtime_contract_for_agent",
    "sanitizePermissionMode",
    "shapeForegroundSubagentResult",
    "startBackgroundSubagent",
    "structured_result_path",
    "subagentReconciliationLogPath",
    "summarize_subagent_runtime_contract",
    "upsertSubagentLaunchGroup",
    "upsertSubagentReconciliation",
    "withSubagentCoordinationTransaction",
]
