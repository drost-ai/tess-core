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
from ..context_policy import decideDurableCheckpoint, estimateRawContextPressure
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

IMAGE_INPUT_UNSUPPORTED_MESSAGE = "This model does not support image inputs."


def requestHasImageInputs(request: dict[str, Any]) -> bool:
    for message in request.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if _content_has_image_input(content):
            return True
    return False


def modelSupportsImageInputs(config: dict[str, Any], model: str) -> bool:
    profile = config.get("providerProfile") or {}
    explicit = _explicit_vision_support(profile)
    if explicit is not None:
        return explicit
    provider = str(config.get("provider") or config.get("providerKind") or "").strip().lower()
    normalized_model = str(model or "").strip().lower()
    if provider == "anthropic":
        return normalized_model.startswith("claude-")
    if provider != "openai":
        return False
    mode = str(config.get("openaiApiMode") or profile.get("apiMode") or "").strip().lower()
    base_url = str(config.get("openaiBaseUrl") or profile.get("baseUrl") or "").strip().lower()
    if mode == "codex" or not base_url or "api.openai.com" in base_url or "chatgpt.com" in base_url:
        return _openai_model_supports_images(normalized_model)
    return _model_name_advertises_vision(normalized_model)


def unsupportedImageInputResponse(model: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": IMAGE_INPUT_UNSUPPORTED_MESSAGE}],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": model,
        "stopReason": "unsupported_image_input",
    }


def _content_has_image_input(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type in ("image", "input_image"):
            return True
    return False


def _explicit_vision_support(profile: dict[str, Any]) -> bool | None:
    for key in ("supportsVision", "supports_vision", "vision"):
        if key in profile and profile.get(key) is not None:
            return bool(profile.get(key))
    capabilities = profile.get("capabilities")
    if isinstance(capabilities, dict):
        for key in ("vision", "image_inputs", "imageInputs", "multimodal"):
            if key in capabilities:
                return bool(capabilities.get(key))
    if isinstance(capabilities, list):
        normalized = {str(item).strip().lower() for item in capabilities}
        if "vision" in normalized or "image_inputs" in normalized or "image-inputs" in normalized:
            return True
    return None


def _openai_model_supports_images(model: str) -> bool:
    if not model:
        return False
    if model.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4")):
        return True
    return _model_name_advertises_vision(model)


def _model_name_advertises_vision(model: str) -> bool:
    markers = ("vision", "vl", "multimodal", "omni", "gpt-4o")
    return any(marker in model for marker in markers)
