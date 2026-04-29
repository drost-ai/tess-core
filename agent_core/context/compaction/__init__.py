from __future__ import annotations

import sys as _sys
import types as _types
from importlib import import_module

_MODULE_NAMES = ['shared', 'orchestration', 'models', 'prompts', 'parsing', 'documents', 'evidence', 'cutoffs', 'artifacts', 'utils']

_modules = [import_module(f"{__name__}.{name}") for name in _MODULE_NAMES]
_namespace = {}
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            _namespace[_name] = _value
for _module in _modules:
    vars(_module).update(_namespace)
globals().update(_namespace)


_PUBLIC_EXPORTS = ['AUTO_SOFT_RETRIGGER_MESSAGE_DELTA', 'AUTO_SOFT_RETRIGGER_TOKEN_DELTA', 'Any', 'COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT', 'COMPACTION_COMPACT_TOOL_RESULT_CHAR_LIMIT', 'COMPACTION_EVIDENCE_FALLBACK_BUDGETS', 'COMPACTION_EVIDENCE_TOKEN_BUDGET', 'COMPACTION_FILE_PREVIEW_LINES', 'COMPACTION_MATCH_PREVIEW_LIMIT', 'COMPACTION_MESSAGE_TEXT_CHAR_LIMIT', 'COMPACTION_STRATEGY', 'COMPACTION_SYNTHESIS_MAX_RETRIES', 'COMPACTION_SYSTEM_PROMPT', 'COMPACTION_TOOL_INPUT_CHAR_LIMIT', 'COMPACTION_TOOL_RESULT_CHAR_LIMIT', 'COMPACTION_VERIFICATION_MAX_RETRIES', 'COMPACTION_VERIFICATION_SYSTEM_PROMPT', 'CONTINUATION_BRIDGE_MESSAGE', 'HARD_WORKING_CONTEXT_TOKEN_LIMIT', 'LOW_VALUE_TAIL_TOOL_NAMES', 'MAX_ACTIVITY_LINES', 'MAX_FAILURES', 'MAX_PRESERVED_TOOL_EXCHANGE_PAIRS', 'MAX_TODOS', 'MIN_COMPACTABLE_MESSAGE_COUNT', 'MIN_PRESERVED_TOOL_EXCHANGE_PAIRS', 'MIN_RECENT_MESSAGE_COUNT', 'RAW_TAIL_TOKEN_BUDGET', 'RAW_TAIL_TOKEN_TARGET', 'SOFT_WORKING_CONTEXT_TOKEN_LIMIT', 'annotations', 'appendActivitySection', 'appendPlanSection', 'appendProjectFactsSection', 'appendPromptList', 'appendRelevantFilesSection', 'appendStringSection', 'appendSubsection', 'asRecord', 'asStringArray', 'assertModelBackedCompactionArtifacts', 'buildArtifactPaths', 'buildCompactionAttemptStrategies', 'buildCompactionEvidence', 'buildCompactionQualityFromCandidate', 'buildCompactionQualityFromSynthesis', 'buildCompactionSynthesisPrompt', 'buildCompactionSystemPromptContext', 'buildCompactionVerificationPrompt', 'buildEvidenceEntriesForMessage', 'buildFilePreview', 'buildMessagesForWorkingContext', 'buildModelBackedCompactionStateSnapshot', 'buildSynthesizedWorkingContextDocument', 'buildTextEvidenceEntry', 'buildToolResultEvidenceEntry', 'buildToolUseEvidenceEntry', 'buildWorkingContextDocumentFromCandidate', 'chooseCompactionCutoff', 'chooseToolAwareCompactionCutoff', 'coerceProjectFacts', 'coerceRelevantFiles', 'coerceRepairedCheckpoint', 'coerceSynthesizedWorkingContextFields', 'coerceWorkingContextDocument', 'collapseWhitespace', 'collectSubstantiveToolExchangeStarts', 'compactStructuredRecord', 'compactionCheckpointPath', 'compactionManifestPath', 'createDefaultCompactionModelClient', 'ensureBaseLayout', 'estimateMessageTokens', 'estimateSingleMessageTokens', 'estimateTextTokens', 'estimateWorkingContextTokens', 'extractJsonObjectCandidate', 'fallbackEligibleProviderFailure', 'getSessionCompactionsDir', 'isCompactionContextSizeFailure', 'isLowSignalTodoText', 'isProviderRequestError', 'isSafeWorkingSetStart', 'json', 'listPlanApprovals', 'loadCompactionManifest', 'loadWorkingContextArtifact', 'mergeCompactionUsageIntoSession', 'messageHasToolResults', 'messageHasToolUses', 'normalizeVerificationConfidence', 'normalizeVerificationVerdict', 'now_iso', 'os', 'parseCompactionSynthesisResponse', 'parseCompactionVerificationResponse', 'parseMaybeJson', 're', 'renderActivityMarkdown', 'renderDecisionsMarkdown', 'renderPlanMarkdown', 'renderSummaryMarkdown', 'renderWorkingContextPromptContext', 'resolveCompactionModelCandidates', 'resolveLatestPlan', 'runCompaction', 'runCompactionCheckpointVerification', 'runCompactionCheckpointVerificationWithFallback', 'runModelBackedCompactionSynthesis', 'runModelBackedCompactionSynthesisWithFallback', 'saveCompactionCheckpoint', 'saveCompactionManifest', 'shouldAutoCompact', 'shouldRetryCompactionAttempt', 'summarizeTodo', 'summarizeToolResultForCompaction', 'trimPathEvidence', 'truncate', 'updateDurableMemoryFromWorkingContext', 'uuid']

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
