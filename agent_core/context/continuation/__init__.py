"""Continuation context and staged relief.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .compaction import (
    applyCompactPrefix,
    applyPartialCompaction,
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
    renderRegionSummaryReinjectionLines,
    renderTodoOverlay,
    summarizeContinuationRegion,
)
from .shared import (
    LOW_VALUE_TOOL_NAMES,
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
    "appendSummarySection",
    "applyCompactPrefix",
    "applyContextReliefMode",
    "applyContextReliefModes",
    "applyLightweightContextRelief",
    "applyPartialCompaction",
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
    "renderRegionSummaryReinjectionLines",
    "renderTodoOverlay",
    "summarizeContinuationRegion",
    "summarizeToolResultForRelief",
    "supportsApiMicrocompact",
    "truncate",
]
