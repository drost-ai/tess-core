from __future__ import annotations

from ..providers.token_budget import (
    normalizePositiveInteger,
    resolveCompactionPolicy,
    resolve_interactive_context_policy as _resolve_interactive_context_policy,
    resolveProviderProfileTokenLimits,
)

import re
from typing import Any

from .._utils import clone
from ..providers.google.thinking import google_thinking_levels, resolve_google_thinking_level
from ..providers.openai import is_codex_base_url
from .defaults import (
    list_provider_profiles_for_kind,
    normalize_effort_level,
    normalize_provider_name,
    normalize_provider_profile_name,
)
from .shared import (
    BUILTIN_PROVIDER_PROFILES,
    DEFAULT_IMAGE_INPUT_MIME_TYPES,
    default_max_aggregate_image_input_bytes,
    default_max_encoded_image_request_bytes,
    default_max_image_input_bytes,
    default_max_image_input_count,
    DEFAULT_OPENAI_API_MODE,
    DEFAULT_PROVIDER,
    DEFAULT_PROVIDER_PROFILE_NAME,
    MAX_PROVIDER_AGGREGATE_IMAGE_INPUT_BYTES,
    MAX_PROVIDER_ENCODED_IMAGE_REQUEST_BYTES,
    MAX_PROVIDER_IMAGE_INPUT_BYTES,
    MAX_PROVIDER_IMAGE_INPUT_COUNT,
    declared_image_input,
    model_implies_image_input,
)

from .credentials import _resolve_anthropic_credentials, _resolve_google_credential, _resolve_openai_credential

def resolve_openai_api_mode(args: dict[str, Any] | None = None) -> str:
    args = dict(args or {})
    requested = args.get("requestedMode")
    if requested and requested != "auto":
        return requested
    if args.get("baseUrl") and is_codex_base_url(args.get("baseUrl")):
        return "codex"
    if args.get("loadedCodexToken") or args.get("storedTokenSource") == "codex-oauth":
        return "codex"
    return DEFAULT_OPENAI_API_MODE

def resolve_provider_profiles(customProfiles: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The built-in provider profiles with custom profiles merged over them, keyed by normalised name."""
    merged = clone(BUILTIN_PROVIDER_PROFILES)
    for raw_name, record in (customProfiles or {}).items():
        name = normalize_provider_profile_name(raw_name)
        if name:
            merged[name] = _normalize_provider_profile_record(record)
    return merged

def resolve_provider_profile(name: str, record: dict[str, Any], overrides: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Resolve one provider profile record into the settings a request needs, such as API mode and
    token limits, applying any overrides.
    """
    if kwargs:
        overrides = {**(overrides or {}), **kwargs}
    overrides = overrides or {}
    api_mode = (
        resolve_openai_api_mode(
            {
                "requestedMode": overrides.get("requestedOpenAiApiMode") or record.get("apiMode"),
                "baseUrl": overrides.get("openaiBaseUrl") or record.get("baseUrl"),
            }
        )
        if record.get("kind") == "openai"
        else None
    )
    token_limits = resolveProviderProfileTokenLimits(
        {
            "profileName": name,
            "profile": record,
            "apiMode": api_mode,
            "model": overrides.get("model"),
        }
    )
    compaction_policy = resolveCompactionPolicy(
        {
            "profileName": name,
            "kind": record.get("kind"),
            "apiMode": api_mode,
            "model": overrides.get("model") or record.get("defaultModel"),
            "maxContextLength": token_limits["maxContextLength"],
            "maxOutputTokens": token_limits["maxOutputTokens"],
            "compactionThresholdTokens": record.get("compactionThresholdTokens"),
            "compactionTargetTokens": record.get("compactionTargetTokens"),
            "compactionOutputTokens": record.get("compactionOutputTokens"),
        }
    )
    interactive_policy = _resolve_interactive_context_policy(
        {
            "profileName": name,
            "kind": record.get("kind"),
            "apiMode": api_mode,
            "model": overrides.get("model") or record.get("defaultModel"),
            "maxContextLength": token_limits["maxContextLength"],
            "maxOutputTokens": token_limits["maxOutputTokens"],
            "compactionThresholdTokens": compaction_policy["compactionThresholdTokens"],
            "compactionTargetTokens": compaction_policy["compactionTargetTokens"],
            "contextReliefThresholdTokens": record.get("contextReliefThresholdTokens"),
            "contextReliefTargetTokens": record.get("contextReliefTargetTokens"),
            "inlineToolResultBatchBytes": record.get("inlineToolResultBatchBytes"),
            "inlineToolResultTurnBytes": record.get("inlineToolResultTurnBytes"),
        }
    )
    return {
        "name": name,
        "kind": record["kind"],
        "apiMode": api_mode,
        "payloadClass": record.get("payloadClass") if record.get("payloadClass") in ("small", "medium", "large") else None,
        "baseUrl": (overrides.get("openaiBaseUrl") if record.get("kind") == "openai" else None) or (record.get("baseUrl") or None),
        "defaultModel": record.get("defaultModel"),
        "auth": clone(record.get("auth") or {}),
        "headers": clone(record.get("headers") or {}),
        "supportsVision": record.get("supportsVision"),
        "capabilities": _resolve_provider_capabilities(record),
        # reasoningEffort is the fallback request effort for main turns
        # (Migel, 2026-08-08); explicit effortLevel/effort keys win.
        "effortLevel": record.get("effortLevel") or record.get("effort") or record.get("reasoningEffort"),
        "reasoningEffortEnabled": record.get("reasoningEffortEnabled"),
        "reasoningEffort": normalize_effort_level(record.get("reasoningEffort")),
        "openAiChatReasoning": clone(record.get("openAiChatReasoning") or {}),
        "openAiChatReasoningSummary": _normalize_openai_reasoning_summary(record.get("openAiChatReasoningSummary")),
        "anthropicThinkingDisplay": _normalize_anthropic_thinking_display(record.get("anthropicThinkingDisplay")),
        "chatTemplateKwargs": clone(record.get("chatTemplateKwargs") or {}),
        "requestStartTimeoutMs": normalizePositiveInteger(record.get("requestStartTimeoutMs")),
        "streamIdleTimeoutMs": normalizePositiveInteger(record.get("streamIdleTimeoutMs")),
        "tokenCountTimeoutMs": normalizePositiveInteger(record.get("tokenCountTimeoutMs")),
        "totalRequestTimeoutMs": normalizePositiveInteger(record.get("totalRequestTimeoutMs")),
        "exactTokenCountRequired": record.get("exactTokenCountRequired") is True,
        "maxContextLength": token_limits["maxContextLength"],
        "maxOutputTokens": token_limits["maxOutputTokens"],
        "compactionThresholdTokens": compaction_policy["compactionThresholdTokens"],
        "compactionTargetTokens": compaction_policy["compactionTargetTokens"],
        "compactionOutputTokens": compaction_policy["compactionOutputTokens"],
        **interactive_policy,
        "requestOptions": clone(record.get("requestOptions") or record.get("request_body") or {}),
        "limits": clone(record.get("limits") or {}),
        "description": record.get("description"),
    }

def resolve_fallback_provider_profile_name(kind: str, profiles: dict[str, Any], currentName: str | None = None) -> str:
    if currentName and profiles.get(currentName, {}).get("kind") == kind:
        return currentName
    preferred = {"anthropic": "anthropic-console", "openai": "openai"}.get(kind)
    if preferred and preferred in profiles:
        return preferred
    matches = list_provider_profiles_for_kind(kind, profiles)
    if matches:
        return matches[0]
    if kind == "google":
        # No Google profile is built in: one must be declared, and the default
        # profile is another provider's.
        raise ValueError("No provider profile of kind google is configured; declare one under providerProfiles.")
    return DEFAULT_PROVIDER_PROFILE_NAME

def resolve_selected_provider_profile_name(args: dict[str, Any] | None = None) -> str:
    args = dict(args or {})
    requested_profile_name = normalize_provider_profile_name(args.get("requestedName"))
    profiles = args["profiles"]
    if requested_profile_name and requested_profile_name in profiles:
        return requested_profile_name
    if requested_profile_name in ("codex", "openai-codex"):
        return "codex"
    requested_kind = normalize_provider_name(requested_profile_name)
    inferred_kind = infer_provider_for_model(args.get("requestedModel"))
    effective_kind = requested_kind or inferred_kind or DEFAULT_PROVIDER
    if effective_kind == "anthropic":
        return _resolve_anthropic_alias_profile_name(args)
    if effective_kind == "google":
        return resolve_fallback_provider_profile_name("google", profiles)
    return _resolve_openai_alias_profile_name(args)

def infer_provider_for_model(model: str | None) -> str | None:
    normalized = model.strip().lower() if isinstance(model, str) else ""
    if normalized.startswith("claude-"):
        return "anthropic"
    if normalized.startswith("gemini-"):
        return "google"
    if normalized.startswith("gpt-") or normalized.startswith("o"):
        return "openai"
    return None

def resolve_provider_for_model_selection(provider: str | None, model: str | None) -> str | None:
    return provider or infer_provider_for_model(model)

def ensure_provider_for_model_selection(provider: str | None, model: str | None, fallback: str = DEFAULT_PROVIDER) -> str:
    return resolve_provider_for_model_selection(infer_provider_for_model(model) or provider, model) or fallback

def model_supports_effort(provider: str, model: str) -> bool:
    normalized = model.strip().lower()
    if provider == "anthropic":
        return (
            "fable-5" in normalized
            or "mythos-5" in normalized
            or "opus-5" in normalized
            or "sonnet-5" in normalized
            or "opus-4-" in normalized
            or "sonnet-4-" in normalized
        )
    if provider == "google":
        return bool(google_thinking_levels(normalized))
    return normalized.startswith("gpt-5.4") or normalized.startswith("gpt-5")

def model_supports_max_effort(provider: str, model: str) -> bool:
    if provider != "anthropic":
        return False
    normalized = model.strip().lower()
    return (
        "fable-5" in normalized
        or "mythos-5" in normalized
        or "opus-5" in normalized
        or "sonnet-5" in normalized
        or "opus-4-" in normalized
        or "sonnet-4-6" in normalized
    )

def model_supports_xhigh_effort(provider: str, model: str) -> bool:
    normalized = model.strip().lower()
    if provider == "anthropic":
        # xhigh arrived with Opus 4.7; the 5-family carries the full ladder.
        return (
            "fable-5" in normalized
            or "mythos-5" in normalized
            or "opus-5" in normalized
            or "sonnet-5" in normalized
            or "opus-4-7" in normalized
            or "opus-4-8" in normalized
        )
    if provider == "google":
        return "xhigh" in google_thinking_levels(normalized)
    # xhigh arrived with gpt-5.4; later minors (5.5, 5.6, …) keep it.
    match = re.match(r"gpt-5\.(\d+)", normalized)
    return bool(match and int(match.group(1)) >= 4)

def normalize_effort_for_model(provider: str, model: str, effort: str | None) -> str | None:
    if not effort or not model_supports_effort(provider, model):
        return None
    if provider == "anthropic":
        if effort == "xhigh" and not model_supports_xhigh_effort(provider, model):
            return "high"
        if effort == "max" and not model_supports_max_effort(provider, model):
            return "high"
        return effort
    if provider == "openai":
        if effort == "max":
            return "xhigh" if model_supports_xhigh_effort(provider, model) else "high"
        if effort == "xhigh" and not model_supports_xhigh_effort(provider, model):
            return "high"
        return effort
    if provider == "google":
        # The effort a Gemini model is asked for is one of its own thinking levels.
        return resolve_google_thinking_level(model, effort)
    return None

def normalize_model_name(inputModel: str, providerHint: str | None = None) -> str:
    raw = inputModel.strip()
    provider = providerHint or infer_provider_for_model(raw) or DEFAULT_PROVIDER
    if provider == "anthropic" and raw == "opus":
        return "claude-opus-4-6"
    if provider == "anthropic" and raw == "sonnet":
        return "claude-sonnet-4-6"
    if provider == "anthropic" and raw == "haiku":
        return "claude-haiku-4-5"
    if provider == "openai" and raw in ("gpt", "gpt5", "gpt-5"):
        return "gpt-5.4"
    if provider == "openai" and raw in ("gpt-mini", "gpt-5-mini"):
        return "gpt-5-mini-2025-08-07"
    return raw

def _normalize_provider_profile_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": record["kind"],
        "apiMode": record.get("apiMode"),
        "baseUrl": (record.get("baseUrl") or "").strip() or None,
        "defaultModel": (record.get("defaultModel") or "").strip() or None,
        "auth": {
            "kind": (record.get("auth") or {}).get("kind"),
            "envVar": ((record.get("auth") or {}).get("envVar") or "").strip() or None,
            "headerName": ((record.get("auth") or {}).get("headerName") or "").strip() or None,
        },
        "headers": clone(record.get("headers") or {}),
        "supportsVision": record.get("supportsVision"),
        "capabilities": _normalize_provider_capabilities(record.get("capabilities")),
        "effortLevel": (record.get("effortLevel") or record.get("effort") or record.get("reasoningEffort") or "").strip().lower() or None,
        "reasoningEffortEnabled": record.get("reasoningEffortEnabled"),
        "reasoningEffort": normalize_effort_level(record.get("reasoningEffort")),
        "openAiChatReasoning": _normalize_object_record(record.get("openAiChatReasoning")),
        "openAiChatReasoningSummary": _normalize_openai_reasoning_summary(record.get("openAiChatReasoningSummary")),
        "anthropicThinkingDisplay": _normalize_anthropic_thinking_display(record.get("anthropicThinkingDisplay")),
        "chatTemplateKwargs": _normalize_object_record(record.get("chatTemplateKwargs")),
        "requestStartTimeoutMs": normalizePositiveInteger(record.get("requestStartTimeoutMs")),
        "streamIdleTimeoutMs": normalizePositiveInteger(record.get("streamIdleTimeoutMs")),
        "tokenCountTimeoutMs": normalizePositiveInteger(record.get("tokenCountTimeoutMs")),
        "totalRequestTimeoutMs": normalizePositiveInteger(record.get("totalRequestTimeoutMs")),
        "exactTokenCountRequired": record.get("exactTokenCountRequired") is True,
        "maxContextLength": normalizePositiveInteger(record.get("maxContextLength")),
        "maxOutputTokens": normalizePositiveInteger(record.get("maxOutputTokens")),
        "compactionThresholdTokens": normalizePositiveInteger(record.get("compactionThresholdTokens")),
        "compactionTargetTokens": (
            record.get("compactionTargetTokens")
            if "compactionTargetTokens" in record
            else None
        ),
        "compactionOutputTokens": normalizePositiveInteger(record.get("compactionOutputTokens")),
        "contextReliefThresholdTokens": (
            record.get("contextReliefThresholdTokens")
            if "contextReliefThresholdTokens" in record
            else None
        ),
        "contextReliefTargetTokens": (
            record.get("contextReliefTargetTokens")
            if "contextReliefTargetTokens" in record
            else None
        ),
        "inlineToolResultBatchBytes": (
            record.get("inlineToolResultBatchBytes")
            if "inlineToolResultBatchBytes" in record
            else None
        ),
        "inlineToolResultTurnBytes": (
            record.get("inlineToolResultTurnBytes")
            if "inlineToolResultTurnBytes" in record
            else None
        ),
        "requestOptions": clone(record.get("requestOptions") or record.get("request_body") or {}),
        "limits": clone(record.get("limits") or {}),
        "description": (record.get("description") or "").strip() or None,
    }


def _normalize_provider_capabilities(value: Any) -> dict[str, Any]:
    capabilities = value if isinstance(value, dict) else {}
    raw_mime_types = capabilities.get("imageMimeTypes")
    mime_types: list[str] | None = None
    if isinstance(raw_mime_types, list):
        mime_types = []
        for item in raw_mime_types:
            normalized = str(item).strip().lower() if isinstance(item, str) else ""
            if (
                normalized
                and re.fullmatch(r"image/[a-z0-9.+-]{1,64}", normalized)
                and normalized not in mime_types
            ):
                mime_types.append(normalized)
            if len(mime_types) >= 32:
                break
    normalized = clone(capabilities)
    for key in (
        "imageInput", "imageMimeTypes", "maxImageCount", "maxImageBytes",
        "maxAggregateImageBytes", "maxEncodedImageRequestBytes",
    ):
        normalized.pop(key, None)
    return {
        **normalized,
        **({"imageInput": capabilities["imageInput"]} if isinstance(capabilities.get("imageInput"), bool) else {}),
        **({"imageMimeTypes": mime_types} if mime_types is not None else {}),
        **_normalized_capability_limit(capabilities, "maxImageCount", MAX_PROVIDER_IMAGE_INPUT_COUNT),
        **_normalized_capability_limit(capabilities, "maxImageBytes", MAX_PROVIDER_IMAGE_INPUT_BYTES),
        **_normalized_capability_limit(
            capabilities, "maxAggregateImageBytes", MAX_PROVIDER_AGGREGATE_IMAGE_INPUT_BYTES
        ),
        **_normalized_capability_limit(
            capabilities, "maxEncodedImageRequestBytes", MAX_PROVIDER_ENCODED_IMAGE_REQUEST_BYTES
        ),
    }


def _normalized_capability_limit(value: dict[str, Any], key: str, maximum: int) -> dict[str, int]:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return {}
    return {key: min(maximum, int(raw))}


def _resolve_provider_capabilities(record: dict[str, Any]) -> dict[str, Any]:
    explicit = _normalize_provider_capabilities(record.get("capabilities"))
    return {
        **explicit,
        "imageInput": explicit.get("imageInput", _default_image_input_capability(record)),
        "imageMimeTypes": explicit.get("imageMimeTypes", clone(DEFAULT_IMAGE_INPUT_MIME_TYPES)),
        "maxImageCount": explicit.get("maxImageCount", default_max_image_input_count()),
        "maxImageBytes": explicit.get("maxImageBytes", default_max_image_input_bytes()),
        "maxAggregateImageBytes": explicit.get(
            "maxAggregateImageBytes", default_max_aggregate_image_input_bytes()
        ),
        "maxEncodedImageRequestBytes": explicit.get(
            "maxEncodedImageRequestBytes", default_max_encoded_image_request_bytes()
        ),
    }


def _default_image_input_capability(record: dict[str, Any]) -> bool:
    declared = declared_image_input(record)
    if declared is not None:
        return declared
    return model_implies_image_input(
        record.get("kind"), record.get("defaultModel"), record.get("baseUrl"), record.get("apiMode")
    )

def resolve_compaction_provider_profile_binding(args: dict[str, Any] | None = None) -> dict[str, Any] | None:
    args = dict(args or {})
    profile_name = normalize_provider_profile_name(args.get("requestedProfileName"))
    if not profile_name:
        return None
    registry = args.get("providerRegistry")
    if registry is not None:
        return {"profileName": profile_name, "profile": registry.resolve_profile(profile_name), **registry.resolve_auth(profile_name, environment=args.get("providerAuthEnvironment"), codex_auth_path=args.get("providerAuthPath") if args.get("providerAuthProfile") == profile_name else None)}
    provider_profiles = args.get("providerProfiles") or {}
    record = provider_profiles.get(profile_name)
    if not record:
        available = ", ".join(sorted(provider_profiles))
        raise ValueError(f'Unknown compaction provider profile "{profile_name}". Available profiles: {available}')
    profile = resolve_provider_profile(profile_name, record)
    if profile.get("kind") == "google":
        return {
            "profileName": profile_name,
            "profile": profile,
            "googleApiKey": _resolve_google_credential({"profile": profile, "settings_env": args.get("settings_env") or {}}),
            "customHeaders": clone(profile.get("headers") or {}),
        }
    if profile.get("kind") == "openai":
        return {
            "profileName": profile_name,
            "profile": profile,
            "openaiToken": _resolve_openai_credential(
                {
                    "profile": profile,
                    "settings_env": args.get("settings_env") or {},
                    "storedToken": args.get("openaiStoredToken"),
                    "envDefaultToken": args.get("openaiEnvToken"),
                    "codexToken": args.get("openaiCodexToken"),
                }
            ),
            "customHeaders": clone(profile.get("headers") or {}),
        }
    credentials = _resolve_anthropic_credentials(
        {
            "profile": profile,
            "explicitApiKey": args.get("explicitApiKey"),
            "explicitAuthToken": args.get("explicitAuthToken"),
            "storedApiKey": args.get("storedAnthropicApiKey"),
            "storedAuthToken": args.get("storedAnthropicAuthToken"),
            "settings_env": args.get("settings_env") or {},
        }
    )
    return {
        "profileName": profile_name,
        "profile": profile,
        "apiKey": credentials.get("apiKey"),
        "authToken": credentials.get("authToken"),
        "customHeaders": {**clone(profile.get("headers") or {}), **clone(args.get("anthropicHeaders") or {})},
    }

def _normalize_object_record(value: Any) -> dict[str, Any] | None:
    return clone(value) if isinstance(value, dict) else None

def _normalize_openai_reasoning_summary(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"auto", "concise", "detailed"} else None

def _normalize_anthropic_thinking_display(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"auto", "summarized", "hidden"} else None

def _resolve_anthropic_alias_profile_name(args: dict[str, Any]) -> str:
    prefer_auth_token = (
        (args.get("explicitAnthropicAuthToken") and not args.get("explicitAnthropicApiKey"))
        or args.get("storedAnthropicProvider") == "claudeai"
        or (args.get("storedAnthropicAuthToken") and not args.get("storedAnthropicApiKey"))
    )
    preferred = "anthropic-claude-code" if prefer_auth_token else "anthropic-console"
    if preferred in args["profiles"]:
        return preferred
    return resolve_fallback_provider_profile_name("anthropic", args["profiles"])

def _resolve_openai_alias_profile_name(args: dict[str, Any]) -> str:
    mode = resolve_openai_api_mode(
        {
            "requestedMode": args.get("requestedOpenAiApiMode"),
            "baseUrl": args.get("openaiBaseUrl"),
            "storedTokenSource": args.get("storedOpenAiSource"),
            "loadedCodexToken": args.get("loadedCodexToken"),
        }
    )
    preferred = "codex" if mode == "codex" else "openai"
    if preferred in args["profiles"]:
        return preferred
    return resolve_fallback_provider_profile_name("openai", args["profiles"])
