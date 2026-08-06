"""Continuation context and staged relief.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .compaction import (
    applyCompactPrefix,
    applyPartialCompaction,
    applySessionMemoryCompaction,
    choosePreservedRawStart,
    compactHistoricalTail,
    compactMessageForContinuation,
    compactToolInput,
    findLastPlainUserIndex,
    summarizeToolResultForRelief,
)
from .context import buildContinuationContext
from .parsing import (
    emptyCompactionResult,
    emptyReliefSummary,
    extractCommand,
    extractFilePath,
    omitNone,
    parseStructuredToolResult,
)
from .relief import (
    applyContextReliefMode,
    applyContextReliefModes,
    applyLightweightContextRelief,
    chooseContextReliefModes,
    classifyContextReliefPressure,
    isContextPressureRelieved,
    supportsApiMicrocompact,
)
from .rendering import (
    appendSummarySection,
    normalizeReinjectionLines,
    renderContextReliefPrompt,
    renderContinuationOverlay,
    renderPartialCompactionSummary,
    renderPlanOverlay,
    renderRegionSummaryReinjectionLines,
    renderTodoOverlay,
    summarizeContinuationRegion,
)
from .shared import (
    LOW_VALUE_TOOL_NAMES,
    RELIEF_CONTEXT_TOKEN_LIMIT,
    RELIEF_MESSAGE_TEXT_CHAR_LIMIT,
    RELIEF_PREFERRED_RAW_TAIL_TOKENS,
    RELIEF_PRESERVE_MIN_MESSAGES,
    RELIEF_TOOL_INPUT_CHAR_LIMIT,
    RELIEF_TOOL_RESULT_CHAR_LIMIT,
)
from .tokens import (
    asRecord,
    classifyContextPressure,
    collapseWhitespace,
    estimateMessagesTokens,
    estimateTextTokens,
    estimateTodoAndPlanTokens,
    truncate,
)

__all__ = [
    "LOW_VALUE_TOOL_NAMES",
    "RELIEF_CONTEXT_TOKEN_LIMIT",
    "RELIEF_MESSAGE_TEXT_CHAR_LIMIT",
    "RELIEF_PREFERRED_RAW_TAIL_TOKENS",
    "RELIEF_PRESERVE_MIN_MESSAGES",
    "RELIEF_TOOL_INPUT_CHAR_LIMIT",
    "RELIEF_TOOL_RESULT_CHAR_LIMIT",
    "appendSummarySection",
    "applyCompactPrefix",
    "applyContextReliefMode",
    "applyContextReliefModes",
    "applyLightweightContextRelief",
    "applyPartialCompaction",
    "applySessionMemoryCompaction",
    "asRecord",
    "buildContinuationContext",
    "chooseContextReliefModes",
    "choosePreservedRawStart",
    "classifyContextPressure",
    "classifyContextReliefPressure",
    "collapseWhitespace",
    "compactHistoricalTail",
    "compactMessageForContinuation",
    "compactToolInput",
    "emptyCompactionResult",
    "emptyReliefSummary",
    "estimateMessagesTokens",
    "estimateTextTokens",
    "estimateTodoAndPlanTokens",
    "extractCommand",
    "extractFilePath",
    "findLastPlainUserIndex",
    "isContextPressureRelieved",
    "normalizeReinjectionLines",
    "omitNone",
    "parseStructuredToolResult",
    "renderContextReliefPrompt",
    "renderContinuationOverlay",
    "renderPartialCompactionSummary",
    "renderPlanOverlay",
    "renderRegionSummaryReinjectionLines",
    "renderTodoOverlay",
    "summarizeContinuationRegion",
    "summarizeToolResultForRelief",
    "supportsApiMicrocompact",
    "truncate",
]
