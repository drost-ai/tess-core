from __future__ import annotations

from ..provider_token_budget import (
    normalizePositiveInteger,
    resolveCompactionPolicy,
    resolveProviderProfileTokenLimits,
)

from .defaults import normalizeEffortLevel
from .shared import *

def resolveOpenAiApiMode(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    requested = args.get("requestedMode")
    if requested and requested != "auto":
        return requested
    if args.get("baseUrl") and isCodexBaseUrl(args.get("baseUrl")):
        return "codex"
    if args.get("loadedCodexToken") or args.get("storedTokenSource") == "codex-oauth":
        return "codex"
    return DEFAULT_OPENAI_API_MODE

def resolveProviderProfiles(customProfiles: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    merged = clone(BUILTIN_PROVIDER_PROFILES)
    for raw_name, record in (customProfiles or {}).items():
        name = normalizeProviderProfileName(raw_name)
        if name:
            merged[name] = _normalize_provider_profile_record(record)
    return merged

def resolveProviderProfile(name: str, record: dict[str, Any], overrides: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    if kwargs:
        overrides = {**(overrides or {}), **kwargs}
    overrides = overrides or {}
    api_mode = (
        resolveOpenAiApiMode(
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
            "compactionOutputTokens": record.get("compactionOutputTokens"),
        }
    )
    return {
        "name": name,
        "kind": record["kind"],
        "apiMode": api_mode,
        "baseUrl": (overrides.get("openaiBaseUrl") if record.get("kind") == "openai" else None) or (record.get("baseUrl") or None),
        "defaultModel": record.get("defaultModel"),
        "auth": clone(record.get("auth") or {}),
        "headers": clone(record.get("headers") or {}),
        "supportsVision": record.get("supportsVision"),
        "capabilities": clone(record.get("capabilities") or {}),
        "effortLevel": record.get("effortLevel") or record.get("effort"),
        "reasoningEffortEnabled": record.get("reasoningEffortEnabled"),
        "reasoningEffort": normalizeEffortLevel(record.get("reasoningEffort")),
        "openAiChatReasoning": clone(record.get("openAiChatReasoning") or {}),
        "openAiChatReasoningSummary": _normalize_openai_reasoning_summary(record.get("openAiChatReasoningSummary")),
        "anthropicThinkingDisplay": _normalize_anthropic_thinking_display(record.get("anthropicThinkingDisplay")),
        "chatTemplateKwargs": clone(record.get("chatTemplateKwargs") or {}),
        "requestStartTimeoutMs": normalizePositiveInteger(record.get("requestStartTimeoutMs")),
        "streamIdleTimeoutMs": normalizePositiveInteger(record.get("streamIdleTimeoutMs")),
        "maxContextLength": token_limits["maxContextLength"],
        "maxOutputTokens": token_limits["maxOutputTokens"],
        "compactionThresholdTokens": compaction_policy["compactionThresholdTokens"],
        "compactionOutputTokens": compaction_policy["compactionOutputTokens"],
        "requestOptions": clone(record.get("requestOptions") or record.get("request_body") or {}),
        "limits": clone(record.get("limits") or {}),
        "description": record.get("description"),
    }

def resolveFallbackProviderProfileName(kind: str, profiles: dict[str, Any], currentName: str | None = None) -> str:
    if currentName and profiles.get(currentName, {}).get("kind") == kind:
        return currentName
    preferred = "anthropic-console" if kind == "anthropic" else "openai"
    if preferred in profiles:
        return preferred
    matches = listProviderProfilesForKind(kind, profiles)
    return matches[0] if matches else DEFAULT_PROVIDER_PROFILE_NAME

def resolveSelectedProviderProfileName(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    requested_profile_name = normalizeProviderProfileName(args.get("requestedName"))
    profiles = args["profiles"]
    if requested_profile_name and requested_profile_name in profiles:
        return requested_profile_name
    if requested_profile_name in ("codex", "openai-codex"):
        return "codex"
    requested_kind = normalizeProviderName(requested_profile_name)
    inferred_kind = inferProviderForModel(args.get("requestedModel"))
    effective_kind = requested_kind or inferred_kind or DEFAULT_PROVIDER
    if effective_kind == "anthropic":
        return _resolve_anthropic_alias_profile_name(args)
    return _resolve_openai_alias_profile_name(args)

def inferProviderForModel(model: str | None) -> str | None:
    normalized = model.strip().lower() if isinstance(model, str) else ""
    if normalized.startswith("claude-"):
        return "anthropic"
    if normalized.startswith("gpt-") or normalized.startswith("o"):
        return "openai"
    return None

def resolveProviderForModelSelection(provider: str | None, model: str | None) -> str | None:
    return provider or inferProviderForModel(model)

def ensureProviderForModelSelection(provider: str | None, model: str | None, fallback: str = DEFAULT_PROVIDER) -> str:
    return resolveProviderForModelSelection(inferProviderForModel(model) or provider, model) or fallback

def modelSupportsEffort(provider: str, model: str) -> bool:
    normalized = model.strip().lower()
    if provider == "anthropic":
        return "opus-4-" in normalized or "sonnet-4-" in normalized
    return normalized.startswith("gpt-5.4") or normalized.startswith("gpt-5")

def modelSupportsMaxEffort(provider: str, model: str) -> bool:
    return provider == "anthropic" and "opus-4-" in model.strip().lower()

def modelSupportsXhighEffort(provider: str, model: str) -> bool:
    return provider == "openai" and model.strip().lower().startswith("gpt-5.4")

def normalizeEffortForModel(provider: str, model: str, effort: str | None) -> str | None:
    if not effort or not modelSupportsEffort(provider, model):
        return None
    if provider == "anthropic":
        if effort == "xhigh":
            return "high"
        if effort == "max" and not modelSupportsMaxEffort(provider, model):
            return "high"
        return effort
    if provider == "openai":
        if effort == "max":
            return "xhigh" if modelSupportsXhighEffort(provider, model) else "high"
        if effort == "xhigh" and not modelSupportsXhighEffort(provider, model):
            return "high"
        return effort
    return None

def normalizeModelName(inputModel: str, providerHint: str | None = None) -> str:
    raw = inputModel.strip()
    provider = providerHint or inferProviderForModel(raw) or DEFAULT_PROVIDER
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
        "capabilities": clone(record.get("capabilities") or {}),
        "effortLevel": (record.get("effortLevel") or record.get("effort") or "").strip().lower() or None,
        "reasoningEffortEnabled": record.get("reasoningEffortEnabled"),
        "reasoningEffort": normalizeEffortLevel(record.get("reasoningEffort")),
        "openAiChatReasoning": _normalize_object_record(record.get("openAiChatReasoning")),
        "openAiChatReasoningSummary": _normalize_openai_reasoning_summary(record.get("openAiChatReasoningSummary")),
        "anthropicThinkingDisplay": _normalize_anthropic_thinking_display(record.get("anthropicThinkingDisplay")),
        "chatTemplateKwargs": _normalize_object_record(record.get("chatTemplateKwargs")),
        "requestStartTimeoutMs": normalizePositiveInteger(record.get("requestStartTimeoutMs")),
        "streamIdleTimeoutMs": normalizePositiveInteger(record.get("streamIdleTimeoutMs")),
        "maxContextLength": normalizePositiveInteger(record.get("maxContextLength")),
        "maxOutputTokens": normalizePositiveInteger(record.get("maxOutputTokens")),
        "compactionThresholdTokens": normalizePositiveInteger(record.get("compactionThresholdTokens")),
        "compactionOutputTokens": normalizePositiveInteger(record.get("compactionOutputTokens")),
        "requestOptions": clone(record.get("requestOptions") or record.get("request_body") or {}),
        "limits": clone(record.get("limits") or {}),
        "description": (record.get("description") or "").strip() or None,
    }

def resolveCompactionProviderProfileBinding(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
    args = {**(args or {}), **kwargs}
    profile_name = normalizeProviderProfileName(args.get("requestedProfileName"))
    if not profile_name:
        return None
    provider_profiles = args.get("providerProfiles") or {}
    record = provider_profiles.get(profile_name)
    if not record:
        available = ", ".join(sorted(provider_profiles))
        raise ValueError(f'Unknown compaction provider profile "{profile_name}". Available profiles: {available}')
    profile = resolveProviderProfile(profile_name, record)
    if profile.get("kind") == "openai":
        return {
            "profileName": profile_name,
            "profile": profile,
            "openaiToken": _resolve_openai_credential(
                {
                    "profile": profile,
                    "settingsEnv": args.get("settingsEnv") or {},
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
            "settingsEnv": args.get("settingsEnv") or {},
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

def _get_stored_anthropic_auth_token(auth: dict[str, Any] | None) -> str | None:
    if not auth:
        return None
    return auth.get("anthropicAuthToken") or auth.get("authToken") or (auth.get("apiKey") if auth.get("provider") == "claudeai" and _looks_like_claude_ai_token(auth.get("apiKey")) else None)

def _get_stored_anthropic_api_key(auth: dict[str, Any] | None) -> str | None:
    if not auth:
        return None
    token = _get_stored_anthropic_auth_token(auth)
    return auth.get("anthropicApiKey") or (auth.get("apiKey") if auth.get("apiKey") != token else None)

def _resolve_provider_credential_env_value(env_var: str | None, settings_env: dict[str, str]) -> str | None:
    if not env_var:
        return None
    return os.environ.get(env_var) or settings_env.get(env_var)

def _resolve_openai_credential(args: dict[str, Any]) -> str | None:
    profile = args["profile"]
    if profile.get("kind") != "openai":
        return None
    env_var = profile.get("auth", {}).get("envVar") or "OPENAI_API_KEY"
    env_token = _resolve_provider_credential_env_value(env_var, args["settingsEnv"])
    kind = profile.get("auth", {}).get("kind")
    if kind == "custom_bearer":
        return env_token
    if kind == "openai_api_key":
        return env_token or (args.get("storedToken") or args.get("envDefaultToken") if env_var == "OPENAI_API_KEY" else None)
    if kind == "openai_api_key_or_codex":
        return args.get("codexToken") or env_token or (args.get("storedToken") or args.get("envDefaultToken") if env_var == "OPENAI_API_KEY" else None)
    return env_token or (args.get("storedToken") or args.get("envDefaultToken") if env_var == "OPENAI_API_KEY" else None) or args.get("codexToken")

def _resolve_anthropic_credentials(args: dict[str, Any]) -> dict[str, str | None]:
    profile = args["profile"]
    if profile.get("kind") != "anthropic":
        return {}
    env_var = profile.get("auth", {}).get("envVar")
    if profile.get("auth", {}).get("kind") == "anthropic_auth_token":
        token = _resolve_provider_credential_env_value(env_var, args["settingsEnv"]) or args.get("explicitAuthToken") or (args.get("storedAuthToken") if env_var in ("ANTHROPIC_AUTH_TOKEN", None) else None)
        return {"authToken": token}
    api_key = _resolve_provider_credential_env_value(env_var, args["settingsEnv"]) or args.get("explicitApiKey") or (args.get("storedApiKey") if env_var in ("ANTHROPIC_API_KEY", None) else None)
    return {"apiKey": api_key}

def _resolve_anthropic_alias_profile_name(args: dict[str, Any]) -> str:
    prefer_auth_token = (
        (args.get("explicitAnthropicAuthToken") and not args.get("explicitAnthropicApiKey"))
        or args.get("storedAnthropicProvider") == "claudeai"
        or (args.get("storedAnthropicAuthToken") and not args.get("storedAnthropicApiKey"))
    )
    preferred = "anthropic-claude-code" if prefer_auth_token else "anthropic-console"
    if preferred in args["profiles"]:
        return preferred
    return resolveFallbackProviderProfileName("anthropic", args["profiles"])

def _resolve_openai_alias_profile_name(args: dict[str, Any]) -> str:
    mode = resolveOpenAiApiMode(
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
    return resolveFallbackProviderProfileName("openai", args["profiles"])
