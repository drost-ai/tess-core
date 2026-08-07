"""The agent turn engine: loop, provider turns, tool dispatch, checkpoints.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .checkpoints import (
    maybeAutoCheckpointRawSession,
    maybeAutoCompactSession,
)
from .context import (
    emitContinuationRelief,
    isContextWindowFailure,
    prepareProviderRequest,
    recordContinuationRelief,
)
from .context_budget import (
    applyProviderTokenBudget,
    assessContextHeadroom,
    enforceContextHeadroom,
    headroomEventPayload,
)
from .dynamic_context import (
    dynamicContextBelongsInMessages,
    injectSystemContextIntoMessages,
)
from .continuity import readOpenChildQuestions, runParentContinuityTurn
from .loop import runAgentPrompt
from .plan_policy import (
    OperatorPolicyDecision,
    bounded_mcp_identity,
    plan_mode_denial,
    resolve_plan_mode_mcp_verdict,
)
from .progress import (
    ARTIFACT_ACQUISITION_FAILURE_LIMIT,
    GENERAL_NO_PROGRESS_LIMIT,
    LogicalTurnProgressLedger,
    MAX_RECORDED_ARTIFACTS,
    MAX_RECORDED_FINGERPRINTS,
    MAX_REPEATED_CYCLE_BATCHES,
    PROGRESS_LEDGER_SCHEMA_VERSION,
    REPEATED_TOOL_CYCLE_LIMIT,
    appendToolResultBatch,
    noProgressEventData,
    noProgressTurnResult,
)
from .prompt_content import (
    extractAssistantText,
    normalize_agent_prompt,
)
from .provider_turn import (
    addEmptyResponseContinuationNudge,
    completeEmptyProviderResponse,
    isEmptyAssistantResponse,
    recordEmptyResponseUsage,
    requestProviderResponse,
)
from .request_integrity import (
    STRICT_TOOL_PROTOCOL_PROVIDERS,
    normalizeProviderRequestMessages,
)
from .shared import (
    IMAGE_INPUT_UNSUPPORTED_MESSAGE,
    PLAN_SAFE_TOOL_NAMES,
    modelSupportsImageInputs,
    requestHasImageInputs,
    unsupportedImageInputResponse,
)
from .stable_boundary import (
    finish_stable_boundary,
    run_stable_boundary_hook,
)
from .tool_context import buildToolContext
from .tool_dispatch import (
    SerialQueue,
    ToolDispatchOutcome,
    approval_skip_result,
    create_serial_queue,
    invalid_tool_input_message,
    resolve_tool_concurrency,
    run_tool_dispatch_batch,
)
from .tool_hooks import (
    build_tool_hook_context,
    run_post_tool_hook,
    run_pre_tool_hook,
)
from .tool_result_budget import (
    ToolResultBatchBudget,
    ToolResultTurnBudget,
)
from .turn_session import (
    resolve_effective_provider_turn_binding,
    run_in_provider_turn_session,
    run_with_transient_execution_session,
)

__all__ = [
    "ARTIFACT_ACQUISITION_FAILURE_LIMIT",
    "GENERAL_NO_PROGRESS_LIMIT",
    "IMAGE_INPUT_UNSUPPORTED_MESSAGE",
    "LogicalTurnProgressLedger",
    "MAX_RECORDED_ARTIFACTS",
    "MAX_RECORDED_FINGERPRINTS",
    "MAX_REPEATED_CYCLE_BATCHES",
    "OperatorPolicyDecision",
    "PLAN_SAFE_TOOL_NAMES",
    "PROGRESS_LEDGER_SCHEMA_VERSION",
    "REPEATED_TOOL_CYCLE_LIMIT",
    "STRICT_TOOL_PROTOCOL_PROVIDERS",
    "SerialQueue",
    "ToolDispatchOutcome",
    "ToolResultBatchBudget",
    "ToolResultTurnBudget",
    "addEmptyResponseContinuationNudge",
    "appendToolResultBatch",
    "applyProviderTokenBudget",
    "approval_skip_result",
    "assessContextHeadroom",
    "bounded_mcp_identity",
    "buildToolContext",
    "build_tool_hook_context",
    "completeEmptyProviderResponse",
    "create_serial_queue",
    "dynamicContextBelongsInMessages",
    "emitContinuationRelief",
    "enforceContextHeadroom",
    "extractAssistantText",
    "finish_stable_boundary",
    "headroomEventPayload",
    "injectSystemContextIntoMessages",
    "invalid_tool_input_message",
    "isContextWindowFailure",
    "isEmptyAssistantResponse",
    "maybeAutoCheckpointRawSession",
    "maybeAutoCompactSession",
    "modelSupportsImageInputs",
    "noProgressEventData",
    "noProgressTurnResult",
    "normalizeProviderRequestMessages",
    "normalize_agent_prompt",
    "plan_mode_denial",
    "prepareProviderRequest",
    "recordContinuationRelief",
    "recordEmptyResponseUsage",
    "requestHasImageInputs",
    "requestProviderResponse",
    "resolve_effective_provider_turn_binding",
    "resolve_plan_mode_mcp_verdict",
    "resolve_tool_concurrency",
    "runAgentPrompt",
    "runParentContinuityTurn",
    "readOpenChildQuestions",
    "run_in_provider_turn_session",
    "run_post_tool_hook",
    "run_pre_tool_hook",
    "run_stable_boundary_hook",
    "run_tool_dispatch_batch",
    "run_with_transient_execution_session",
    "unsupportedImageInputResponse",
]
