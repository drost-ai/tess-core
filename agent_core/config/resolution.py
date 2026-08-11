from __future__ import annotations

from ..contracts.config import ResolvedConfig
from ..runtime.secret_provider import (
    normalize_compaction_provider_secrets,
    normalize_provider_headers,
    normalize_runtime_secret_value,
)
from ..sandbox import normalizeSandboxConfig

import os
from typing import Any

from .._utils import clone
from ..storage import (
    configure_runtime_home,
    ensure_base_layout,
    get_context_runtime_home,
    load_auth,
    load_codex_auth_token,
    resolve_maybe_relative,
    resolve_runtime_home,
)
from .defaults import (
    getDefaultEffortForProvider,
    getDefaultModelForProvider,
    normalizeEffortLevel,
    normalizeOpenAiApiModeSetting,
    normalizeProviderName,
    normalizeProviderProfileName,
    normalizeTuiScreenMode,
)
from ._knobs import configureKnobs
from .parsing import (
    _dedupe,
    _load_mcp_config,
    _load_merged_settings,
    _parse_custom_headers,
    _parse_inline_agents,
    _parse_json_schema,
    _parse_setting_sources,
    _parse_tool_list,
    _resolve_api_key_from_helper,
)
from .prompts import buildDefaultSystemPrompt, defaultToolList
from .credentials import (
    _get_stored_anthropic_api_key,
    _get_stored_anthropic_auth_token,
    _resolve_anthropic_credentials,
    _resolve_openai_credential,
)
from .providers import (
    normalizeModelName,
    resolveCompactionProviderProfileBinding,
    resolveProviderForModelSelection,
    resolveProviderProfile,
    resolveProviderProfiles,
    resolveSelectedProviderProfileName,
)
from .shared import (
    BUILTIN_AGENTS,
    BUILTIN_PROVIDER_PROFILES,
    DEFAULT_ASSISTANT_DESCRIPTION,
    DEFAULT_ASSISTANT_NAME,
    DEFAULT_OPENAI_API_MODE,
    DEFAULT_OPENAI_CODEX_AUTH_PATH,
    DEFAULT_PROVIDER_PROFILE_NAME,
    DEFAULT_TUI_SCREEN_MODE,
)

def _opt(options: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    if options is None:
        return default
    return options.get(key, default)

def _omit_provider_credential_environment(
    settings_env: dict[str, str],
    provider_profiles: dict[str, dict[str, Any]],
) -> dict[str, str]:
    secret_keys = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "OPENAI_API_KEY",
    }
    for profile in provider_profiles.values():
        env_var = (profile.get("auth") or {}).get("envVar")
        if isinstance(env_var, str) and env_var:
            secret_keys.add(env_var)
    return {key: value for key, value in settings_env.items() if key not in secret_keys}

def _resolve_runtime_home(options: dict[str, Any], cwd: str) -> str:
    requested = options.get("runtimeHome")
    context = get_context_runtime_home()
    runtime_home = resolve_runtime_home(cwd, requested) if requested else context or resolve_runtime_home(cwd)
    if context and requested and runtime_home != context:
        raise ValueError("runtimeHome conflicts with active scoped runtime home")
    if not context:
        configure_runtime_home(runtime_home)
    return runtime_home

def _resolve_requested_model(
    options: dict[str, Any],
    merged_settings: dict[str, Any],
    provider_profiles: dict[str, dict[str, Any]],
) -> tuple[Any, Any]:
    """Pick the provider/model request pair from options, env, and settings.

    A provider override that names a different profile than the settings file
    intentionally discards the settings model (and env model shortcuts): the
    override selects the profile's own default model instead.
    """
    provider_override_name = options.get("provider") or os.environ.get("TESS_PROVIDER")
    requested_provider_name = provider_override_name or merged_settings.get("provider")
    override_profile_name = normalizeProviderProfileName(provider_override_name)
    settings_profile_name = normalizeProviderProfileName(merged_settings.get("provider"))
    provider_override_selects_profile = bool(
        provider_override_name
        and override_profile_name
        and override_profile_name in provider_profiles
        and override_profile_name != settings_profile_name
    )
    settings_model_input = None if provider_override_selects_profile else merged_settings.get("model")
    direct_model_input = options.get("model") or os.environ.get("TESS_MODEL") or settings_model_input
    explicit_provider_kind = normalizeProviderName(requested_provider_name)
    provider_env_model = None
    if not provider_override_selects_profile:
        provider_env_model = (
            os.environ.get("OPENAI_MODEL")
            if explicit_provider_kind == "openai"
            else os.environ.get("ANTHROPIC_MODEL")
            if explicit_provider_kind == "anthropic"
            else os.environ.get("ANTHROPIC_MODEL") or os.environ.get("OPENAI_MODEL")
        )
    return requested_provider_name, direct_model_input or provider_env_model

def _resolve_compaction_selection(
    options: dict[str, Any],
    settings_env: dict[str, str],
    merged_settings: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    """Resolve the compaction (or compaction-verification) model binding.

    Both bindings layer identically — option, process env, settings env,
    settings file — and differ only in their key prefixes.
    """
    option_prefix = "compaction" if kind == "compaction" else "compactionVerification"
    env_prefix = "TESS_COMPACTION" if kind == "compaction" else "TESS_COMPACTION_VERIFICATION"

    def setting(suffix: str, env_suffix: str) -> Any:
        option_key = f"{option_prefix}{suffix}"
        env_key = f"{env_prefix}_{env_suffix}"
        return (
            options.get(option_key)
            or os.environ.get(env_key)
            or settings_env.get(env_key)
            or merged_settings.get(option_key)
        )

    requested_provider = normalizeProviderName(setting("Provider", "PROVIDER"))
    requested_model = setting("Model", "MODEL")
    provider = (
        resolveProviderForModelSelection(requested_provider, requested_model)
        if requested_model
        else requested_provider
    )
    return {
        "provider": provider,
        "model": normalizeModelName(requested_model, provider) if requested_model and provider else requested_model,
        "effort": normalizeEffortLevel(setting("Effort", "EFFORT")),
        "profileName": setting("ProviderProfile", "PROVIDER_PROFILE"),
    }

def _resolve_screen_mode(
    options: dict[str, Any],
    settings_env: dict[str, str],
    merged_settings: dict[str, Any],
) -> str:
    if options.get("noAltScreen") or options.get("altScreen") is False:
        return "inline"
    return (
        normalizeTuiScreenMode(
            options.get("screenMode")
            or os.environ.get("TESS_SCREEN_MODE")
            or os.environ.get("TESS_ALTERNATE_SCREEN")
            or settings_env.get("TESS_SCREEN_MODE")
            or settings_env.get("TESS_ALTERNATE_SCREEN")
            or merged_settings.get("screenMode")
        )
        or DEFAULT_TUI_SCREEN_MODE
    )

async def resolveConfig(options: dict[str, Any] | None, cwd: str) -> ResolvedConfig:
    options = options or {}
    runtime_home = _resolve_runtime_home(options, cwd)
    await ensure_base_layout(cwd)

    sources = _parse_setting_sources(options.get("settingSources"))
    merged_settings = await _load_merged_settings(sources, cwd, options.get("settings"))
    configureKnobs(merged_settings.get("knobs"))
    from ..providers.token_budget import configure_model_limit_rules

    configure_model_limit_rules(merged_settings.get("modelLimitRules"))
    auth = await load_auth()
    settings_env = merged_settings.get("env") or {}
    helper_auth_value = await _resolve_api_key_from_helper(merged_settings)
    provider_profiles = resolveProviderProfiles(merged_settings.get("providerProfiles"))
    requested_provider_name, requested_model_input = _resolve_requested_model(options, merged_settings, provider_profiles)

    stored_anthropic_auth_token = _get_stored_anthropic_auth_token(auth)
    stored_anthropic_api_key = _get_stored_anthropic_api_key(auth)
    explicit_api_key = os.environ.get("ANTHROPIC_API_KEY") or settings_env.get("ANTHROPIC_API_KEY") or helper_auth_value
    explicit_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or settings_env.get("ANTHROPIC_AUTH_TOKEN")
    openai_codex_auth_path = resolve_maybe_relative(
        cwd,
        os.environ.get("TESS_OPENAI_CODEX_AUTH_PATH")
        or settings_env.get("TESS_OPENAI_CODEX_AUTH_PATH")
        or merged_settings.get("openaiCodexAuthPath")
        or (auth or {}).get("openaiCodexAuthPath")
        or DEFAULT_OPENAI_CODEX_AUTH_PATH,
    )
    openai_env_token = os.environ.get("OPENAI_API_KEY") or settings_env.get("OPENAI_API_KEY")
    openai_stored_token = (auth or {}).get("openaiToken")
    openai_codex_token = await load_codex_auth_token(openai_codex_auth_path)
    openai_base_url = os.environ.get("OPENAI_BASE_URL") or settings_env.get("OPENAI_BASE_URL") or merged_settings.get("openaiBaseUrl")
    requested_openai_api_mode = normalizeOpenAiApiModeSetting(
        options.get("openaiApiMode")
        or os.environ.get("TESS_OPENAI_API_MODE")
        or os.environ.get("OPENAI_API_MODE")
        or settings_env.get("TESS_OPENAI_API_MODE")
        or settings_env.get("OPENAI_API_MODE")
        or merged_settings.get("openaiApiMode")
    )

    selected_name = resolveSelectedProviderProfileName(
        {
            "requestedName": requested_provider_name,
            "requestedModel": requested_model_input,
            "profiles": provider_profiles,
            "requestedOpenAiApiMode": requested_openai_api_mode,
            "openaiBaseUrl": openai_base_url,
            "storedOpenAiSource": (auth or {}).get("openaiSource"),
            "loadedCodexToken": bool(openai_codex_token),
            "explicitAnthropicApiKey": bool(explicit_api_key),
            "explicitAnthropicAuthToken": bool(explicit_auth_token),
            "storedAnthropicApiKey": bool(stored_anthropic_api_key),
            "storedAnthropicAuthToken": bool(stored_anthropic_auth_token),
            "storedAnthropicProvider": (auth or {}).get("anthropicProvider") or (auth or {}).get("provider"),
        }
    )
    selected_profile = resolveProviderProfile(
        selected_name,
        provider_profiles.get(selected_name) or BUILTIN_PROVIDER_PROFILES[DEFAULT_PROVIDER_PROFILE_NAME],
        {"requestedOpenAiApiMode": requested_openai_api_mode, "openaiBaseUrl": openai_base_url},
    )
    resolved_provider = selected_profile["kind"]
    resolved_model = normalizeModelName(
        requested_model_input or selected_profile.get("defaultModel") or getDefaultModelForProvider(resolved_provider),
        resolved_provider,
    )
    selected_profile = resolveProviderProfile(
        selected_name,
        provider_profiles.get(selected_name) or BUILTIN_PROVIDER_PROFILES[DEFAULT_PROVIDER_PROFILE_NAME],
        {"requestedOpenAiApiMode": requested_openai_api_mode, "openaiBaseUrl": openai_base_url, "model": resolved_model},
    )
    openai_api_mode = selected_profile.get("apiMode") if resolved_provider == "openai" else DEFAULT_OPENAI_API_MODE

    compaction = _resolve_compaction_selection(options, settings_env, merged_settings, "compaction")
    verification = _resolve_compaction_selection(options, settings_env, merged_settings, "verification")

    anthropic_headers = {
        **((auth or {}).get("customHeaders") or {}),
        **_parse_custom_headers(settings_env.get("ANTHROPIC_CUSTOM_HEADERS")),
        **_parse_custom_headers(os.environ.get("ANTHROPIC_CUSTOM_HEADERS")),
    }
    custom_headers = {
        **selected_profile.get("headers", {}),
        **(anthropic_headers if resolved_provider == "anthropic" else {}),
    }
    profile_binding_inputs = {
        "providerProfiles": provider_profiles,
        "settings_env": settings_env,
        "openaiStoredToken": openai_stored_token,
        "openaiEnvToken": openai_env_token,
        "openaiCodexToken": openai_codex_token,
        "explicitApiKey": explicit_api_key,
        "explicitAuthToken": explicit_auth_token,
        "storedAnthropicApiKey": stored_anthropic_api_key,
        "storedAnthropicAuthToken": stored_anthropic_auth_token,
        "anthropicHeaders": anthropic_headers,
    }
    compaction_provider_profile = await normalize_compaction_provider_secrets(
        resolveCompactionProviderProfileBinding(
            {"requestedProfileName": compaction["profileName"], **profile_binding_inputs}
        ),
        f"config:{selected_name}:compaction",
    )
    compaction_verification_provider_profile = await normalize_compaction_provider_secrets(
        resolveCompactionProviderProfileBinding(
            {"requestedProfileName": verification["profileName"], **profile_binding_inputs}
        ),
        f"config:{selected_name}:compaction-verification",
    )
    anthropic_creds = _resolve_anthropic_credentials(
        {
            "profile": selected_profile,
            "explicitApiKey": explicit_api_key,
            "explicitAuthToken": explicit_auth_token,
            "storedApiKey": stored_anthropic_api_key,
            "storedAuthToken": stored_anthropic_auth_token,
            "settings_env": settings_env,
        }
    )
    openai_token = _resolve_openai_credential(
        {
            "profile": selected_profile,
            "settings_env": settings_env,
            "storedToken": openai_stored_token,
            "envDefaultToken": openai_env_token,
            "codexToken": openai_codex_token,
        }
    )
    normalized_api_key = await normalize_runtime_secret_value(
        value=anthropic_creds.get("apiKey"),
        purpose="provider_api_key",
        idempotencyScope=f"config:{selected_name}:api-key",
    )
    normalized_auth_token = await normalize_runtime_secret_value(
        value=anthropic_creds.get("authToken"),
        purpose="provider_oauth_token",
        acceptedPurposes=("provider_oauth_token", "provider_setup_token"),
        idempotencyScope=f"config:{selected_name}:auth-token",
    )
    normalized_openai_token = await normalize_runtime_secret_value(
        value=openai_token,
        purpose="provider_oauth_token"
        if isinstance(openai_token, str) and openai_token == openai_codex_token
        else "provider_api_key",
        acceptedPurposes=("provider_api_key", "provider_oauth_token"),
        idempotencyScope=f"config:{selected_name}:openai-token",
    )
    normalized_custom_headers = await normalize_provider_headers(
        custom_headers,
        f"config:{selected_name}",
    )

    tools = _parse_tool_list(options.get("tools"), merged_settings.get("tools"), defaultToolList())
    allowed_tools = _dedupe(_parse_tool_list(options.get("allowedTools"), merged_settings.get("allowedTools"), tools))
    disallowed_tools = _dedupe(_parse_tool_list(options.get("disallowedTools"), merged_settings.get("disallowedTools"), []))
    plugin_dirs = [resolve_maybe_relative(cwd, item) for item in options.get("pluginDir", [])]
    allowed_roots = _dedupe([cwd, *[resolve_maybe_relative(cwd, item) for item in options.get("addDir", [])]])

    agents = {**clone(BUILTIN_AGENTS), **(merged_settings.get("agents") or {}), **(_parse_inline_agents(options.get("agents")) or {})}
    assistant_name = options.get("assistantName") or merged_settings.get("assistantName") or DEFAULT_ASSISTANT_NAME
    assistant_description = options.get("assistantDescription") or merged_settings.get("assistantDescription") or DEFAULT_ASSISTANT_DESCRIPTION
    # Settings-layer prompts come only from settings/system_prompt*.md files
    # (systemPromptFile / systemPromptAppendFile); systemPrompt keys inside
    # settings records are deliberately not consulted.
    system_prompt = (
        options.get("systemPrompt")
        or merged_settings.get("systemPromptFile")
        or options.get("defaultSystemPrompt")
        or buildDefaultSystemPrompt({"assistantName": assistant_name, "assistantDescription": assistant_description})
    )
    settings_sandbox = merged_settings.get("sandbox") if isinstance(merged_settings.get("sandbox"), dict) else {}
    options_sandbox = options.get("sandbox") if isinstance(options.get("sandbox"), dict) else {}
    sandbox_config = normalizeSandboxConfig({**settings_sandbox, **options_sandbox}, cwd)
    settings_subagents = merged_settings.get("subagents") if isinstance(merged_settings.get("subagents"), dict) else {}
    options_subagents = options.get("subagents") if isinstance(options.get("subagents"), dict) else {}
    subagents_config = {**settings_subagents, **options_subagents}
    max_subagent_depth = options.get("maxSubagentDepth") or merged_settings.get("maxSubagentDepth")
    if max_subagent_depth is not None and "maxDepth" not in subagents_config:
        subagents_config["maxDepth"] = max_subagent_depth
    settings_web_search = merged_settings.get("webSearch") if isinstance(merged_settings.get("webSearch"), dict) else merged_settings.get("web_search") if isinstance(merged_settings.get("web_search"), dict) else {}
    options_web_search = options.get("webSearch") if isinstance(options.get("webSearch"), dict) else options.get("web_search") if isinstance(options.get("web_search"), dict) else {}
    web_search_config = {**settings_web_search, **options_web_search}

    strict_mcp = bool(options.get("strictMcpConfig"))
    return {
        "cwd": cwd,
        "runtimeHome": runtime_home,
        "allowedRoots": allowed_roots,
        "agent": options.get("agent"),
        "agents": agents,
        "provider": resolved_provider,
        "providerKind": resolved_provider,
        "providerProfileName": selected_profile["name"],
        "providerProfile": selected_profile,
        "providerCapabilities": clone(selected_profile.get("capabilities") or {}),
        "providerProfiles": {name: resolveProviderProfile(name, record) for name, record in provider_profiles.items()},
        "compactionProvider": compaction["provider"],
        "compactionEffort": compaction["effort"],
        "compactionProviderProfile": compaction_provider_profile,
        "apiKey": normalized_api_key,
        "authToken": normalized_auth_token,
        "customHeaders": normalized_custom_headers,
        "anthropicBaseUrl": selected_profile.get("baseUrl") if resolved_provider == "anthropic" else None,
        "openaiToken": normalized_openai_token,
        "openaiApiMode": openai_api_mode,
        "openaiBaseUrl": selected_profile.get("baseUrl") if resolved_provider == "openai" else None,
        "openaiCodexAuthPath": openai_codex_auth_path,
        "model": resolved_model,
        "compactionModel": compaction["model"],
        "compactionVerificationProvider": verification["provider"],
        "compactionVerificationModel": verification["model"],
        "compactionVerificationEffort": verification["effort"],
        "compactionVerificationProviderProfile": compaction_verification_provider_profile,
        "effort": normalizeEffortLevel(options.get("effort") or os.environ.get("TESS_EFFORT_LEVEL") or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL") or selected_profile.get("effortLevel") or merged_settings.get("effortLevel")) or getDefaultEffortForProvider(resolved_provider),
        "assistantName": assistant_name,
        "assistantDescription": assistant_description,
        "systemPrompt": system_prompt,
        "appendSystemPrompt": options.get("appendSystemPrompt") or merged_settings.get("systemPromptAppendFile"),
        "sandbox": sandbox_config,
        "subagents": subagents_config,
        "webSearch": web_search_config,
        "settings_env": _omit_provider_credential_environment(settings_env, provider_profiles),
        "tools": tools,
        "allowedTools": allowed_tools,
        "disallowedTools": disallowed_tools,
        "inputFormat": options.get("inputFormat") or "text",
        "outputFormat": options.get("outputFormat") or "text",
        "permissionMode": options.get("permissionMode") or merged_settings.get("permissionMode") or "default",
        "screenMode": _resolve_screen_mode(options, settings_env, merged_settings),
        "sessionPersistence": options.get("sessionPersistence", True),
        "jsonSchema": _parse_json_schema(options.get("jsonSchema")),
        "maxBudgetUsd": float(options["maxBudgetUsd"]) if options.get("maxBudgetUsd") else None,
        "fallbackModel": normalizeModelName(options["fallbackModel"]) if options.get("fallbackModel") else None,
        "debugEnabled": bool(options.get("debug") or options.get("debugFile")),
        "debugFilter": options.get("debug") if isinstance(options.get("debug"), str) else None,
        "verbose": bool(options.get("verbose")),
        "bare": bool(options.get("bare")),
        "settingsSources": sources,
        "inlineSettings": options.get("settings"),
        "mcpConfig": await _load_mcp_config(cwd, options.get("mcpConfig"), sources, strict_mcp),
        "mcpConfigPolicy": {"mode": "explicit_only" if strict_mcp else "merged"},
        "channels": _dedupe(options.get("channels") or []),
        "developmentChannels": _dedupe(options.get("dangerouslyLoadDevelopmentChannels") or []),
        "pluginDirs": plugin_dirs,
        "worktree": options.get("worktree"),
        "name": options.get("name"),
    }

