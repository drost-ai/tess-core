from __future__ import annotations

import asyncio
import copy
import re
from typing import Any

from .._utils import maybe_await, merge_usage
from ..config import (
    buildPlanModeSystemPrompt,
    ensureProviderForModelSelection,
    filterToolsByPolicy,
    normalizeEffortForModel,
    resolveConfig,
    resolveFallbackProviderProfileName,
)
from ..compaction import runCompaction, shouldAutoCompact
from ..continuation import buildContinuationContext
from ..provider_errors import fallbackEligibleProviderFailure, isProviderRequestError
from ..providers.anthropic import sendAnthropicMessagesRequest
from ..providers.openai import sendOpenAiRequest
from ..session import persistProviderFailure, persistProviderRecovery, persistSession, repairSessionTranscript
from ..token_accounting import measureRequestInputTokens, recordActualTokenUsage, recordPreflightTokenMeasurement


MODEL_PRICING = {
    "claude-opus-4-6": (5, 25, 6.25, 0.5),
    "claude-sonnet-4-6": (3, 15, 3.75, 0.3),
    "claude-haiku-4-5": (1, 5, 1.25, 0.1),
    "gpt-5.4": (2.5, 15, 0, 0),
    "gpt-5-mini-2025-08-07": (0.25, 2, 0, 0),
}

PLAN_SAFE_TOOL_NAMES = {
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "SetStatus",
    "Agent",
    "AskUserQuestion",
    "Config",
    "ExitPlanMode",
    "TaskMonitorStart",
    "TaskMonitorStop",
    "Mcp",
    "ListMcpResources",
    "ReadMcpResource",
}
