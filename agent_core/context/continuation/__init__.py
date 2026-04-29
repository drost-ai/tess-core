from __future__ import annotations

import sys as _sys
import types as _types
from importlib import import_module

_MODULE_NAMES = ['shared', 'context', 'relief', 'compaction', 'rendering', 'parsing', 'tokens']

_modules = [import_module(f"{__name__}.{name}") for name in _MODULE_NAMES]
_namespace = {}
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            _namespace[_name] = _value
for _module in _modules:
    vars(_module).update(_namespace)
globals().update(_namespace)


_PUBLIC_EXPORTS = ['Any', 'LOW_VALUE_TOOL_NAMES', 'RELIEF_IDLE_GAP_MS', 'RELIEF_MESSAGE_TEXT_CHAR_LIMIT', 'RELIEF_PREFERRED_RAW_TAIL_TOKENS', 'RELIEF_PRESERVE_MIN_MESSAGES', 'RELIEF_PRESSURE_API_MICROCOMPACT_TOKENS', 'RELIEF_PRESSURE_MICROCOMPACT_TOKENS', 'RELIEF_PRESSURE_PARTIAL_TOKENS', 'RELIEF_PRESSURE_SESSION_MEMORY_TOKENS', 'RELIEF_TOOL_INPUT_CHAR_LIMIT', 'RELIEF_TOOL_RESULT_CHAR_LIMIT', 'annotations', 'appendSummarySection', 'applyCompactPrefix', 'applyContextReliefMode', 'applyContextReliefModes', 'applyLightweightContextRelief', 'applyPartialCompaction', 'applySessionMemoryCompaction', 'asRecord', 'buildCompactionSystemPromptContext', 'buildContinuationContext', 'buildMessagesForWorkingContext', 'chooseContextReliefModes', 'choosePreservedRawStart', 'classifyContextPressure', 'classifyContextReliefPressure', 'collapseWhitespace', 'compactHistoricalTail', 'compactMessageForContinuation', 'compactToolInput', 'datetime', 'emptyCompactionResult', 'emptyReliefSummary', 'estimateMessagesTokens', 'estimateTextTokens', 'estimateTodoAndPlanTokens', 'extractCommand', 'extractFilePath', 'findLastPlainUserIndex', 'isContextPressureRelieved', 'json', 'normalizeReinjectionLines', 'omitNone', 'parseStructuredToolResult', 're', 'renderContextReliefPrompt', 'renderContinuationOverlay', 'renderPartialCompactionSummary', 'renderPlanOverlay', 'renderRegionSummaryReinjectionLines', 'renderRetrievedMemoryPrompt', 'renderTodoOverlay', 'retrieveRelevantDurableMemory', 'summarizeContinuationRegion', 'summarizeToolResultForRelief', 'supportsApiMicrocompact', 'timezone', 'truncate']

__all__ = _PUBLIC_EXPORTS


def __dir__():
    return sorted(_PUBLIC_EXPORTS)


class _ContextFacadeModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _modules:
            if name in vars(module):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _ContextFacadeModule
