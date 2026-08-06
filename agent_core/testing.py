"""Test-support surface: the internals integration tests exercise directly.

Aggregated here so tests have one import point for cross-subsystem seams
(compaction planning, provider wire builders, and the *ForTests hooks).
"""
from __future__ import annotations

from .authority.executable_closure import (
    __setExecutableClosureTestHookForTests,
    inspectExecutableClosure,
    snapshotExecutableClosure,
)
from .collaboration.bridge.recovery import __setBridgeRecoveryTestHookForTests
from .context.compaction import (
    buildCompactionSystemPromptContext,
    buildMessagesForWorkingContext,
    estimateWorkingContextTokens,
    runCompaction,
    shouldAutoCompact,
)
from .context.compaction_models import (
    createDefaultCompactionModelClient,
    resolveCompactionExecutionPlan,
    resolveCompactionModelCandidates,
    resolveCompactionTokenPolicy,
)
from .context.continuation import applyContextReliefModes, applyLightweightContextRelief, classifyContextPressure
from .context.token_accounting import measureRequestInputTokens
from .providers.anthropic import (
    buildAnthropicCountTokensPayload,
    buildAnthropicHeaders,
    buildAnthropicMessagesPayload,
    normalizeAnthropicSseResponse,
    resolveAnthropicCountTokensUrl,
    resolveAnthropicMessagesUrl,
)
from .providers.errors import createProviderFailureRecord, createProviderRequestError
from .providers.openai import (
    DEFAULT_CODEX_BASE_URL,
    buildChatCompletionsCreateParams,
    buildResponsesCreateParams,
    sendOpenAiChatCompletionsRequest,
    sendOpenAiResponsesRequest,
)
from .runtime.secrets import resolveRuntimeSecretString
from .tasks.health_check_revalidation import (
    __setHealthCheckGrantClockForTests,
    __setHealthCheckRevalidationTestHookForTests,
)
