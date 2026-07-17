from __future__ import annotations

from ..contracts.config import ResolvedConfig
from ..runtime_secret_provider import (
    normalizeCompactionProviderSecrets,
    normalizeProviderHeaders,
    normalizeRuntimeSecretValue,
)
from ..permission_rules import loadPermissionRules
from ..hooks import loadHooks
from ..sandbox import normalizeSandboxConfig

from .shared import *

def _opt(options: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    if options is None:
        return default
    return options.get(key, default)

def _looks_like_claude_ai_token(value: Any) -> bool:
    return bool(isinstance(value, str) and re.match(r"^sk-ant-o", value, re.I))


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

async def resolveConfig(options: dict[str, Any] | None, cwd: str) -> ResolvedConfig:
    options = options or {}
    requested_runtime_home = options.get("runtimeHome")
    context_runtime_home = getContextRuntimeHome()
    runtime_home = resolveRuntimeHome(cwd, requested_runtime_home) if requested_runtime_home else context_runtime_home or resolveRuntimeHome(cwd)
    if context_runtime_home and requested_runtime_home and runtime_home != context_runtime_home:
        raise ValueError("runtimeHome conflicts with active scoped runtime home")
    if not context_runtime_home:
        configureRuntimeHome(runtime_home)
    await ensureBaseLayout(cwd)

    sources = _parse_setting_sources(options.get("settingSources"))
    merged_settings = await _load_merged_settings(sources, cwd, options.get("settings"))
    auth = await loadAuth()
    settings_env = merged_settings.get("env") or {}
    helper_auth_value = await _resolve_api_key_from_helper(merged_settings)
    provider_profiles = resolveProviderProfiles(merged_settings.get("providerProfiles"))

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
    requested_model_input = direct_model_input or provider_env_model

    stored_anthropic_auth_token = _get_stored_anthropic_auth_token(auth)
    stored_anthropic_api_key = _get_stored_anthropic_api_key(auth)
    explicit_api_key = os.environ.get("ANTHROPIC_API_KEY") or settings_env.get("ANTHROPIC_API_KEY") or helper_auth_value
    explicit_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or settings_env.get("ANTHROPIC_AUTH_TOKEN")
    openai_codex_auth_path = resolveMaybeRelative(
        cwd,
        os.environ.get("TESS_OPENAI_CODEX_AUTH_PATH")
        or settings_env.get("TESS_OPENAI_CODEX_AUTH_PATH")
        or merged_settings.get("openaiCodexAuthPath")
        or (auth or {}).get("openaiCodexAuthPath")
        or DEFAULT_OPENAI_CODEX_AUTH_PATH,
    )
    openai_env_token = os.environ.get("OPENAI_API_KEY") or settings_env.get("OPENAI_API_KEY")
    openai_stored_token = (auth or {}).get("openaiToken")
    openai_codex_token = await loadCodexAuthToken(openai_codex_auth_path)
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

    requested_compaction_provider = normalizeProviderName(
        os.environ.get("TESS_COMPACTION_PROVIDER")
        or settings_env.get("TESS_COMPACTION_PROVIDER")
        or merged_settings.get("compactionProvider")
    )
    requested_compaction_model = (
        os.environ.get("TESS_COMPACTION_MODEL")
        or settings_env.get("TESS_COMPACTION_MODEL")
        or merged_settings.get("compactionModel")
    )
    resolved_compaction_provider = (
        resolveProviderForModelSelection(requested_compaction_provider, requested_compaction_model)
        if requested_compaction_model
        else requested_compaction_provider
    )
    resolved_compaction_model = (
        normalizeModelName(requested_compaction_model, resolved_compaction_provider)
        if requested_compaction_model and resolved_compaction_provider
        else requested_compaction_model
    )

    requested_verification_provider = normalizeProviderName(
        os.environ.get("TESS_COMPACTION_VERIFICATION_PROVIDER")
        or settings_env.get("TESS_COMPACTION_VERIFICATION_PROVIDER")
        or merged_settings.get("compactionVerificationProvider")
    )
    requested_verification_model = (
        os.environ.get("TESS_COMPACTION_VERIFICATION_MODEL")
        or settings_env.get("TESS_COMPACTION_VERIFICATION_MODEL")
        or merged_settings.get("compactionVerificationModel")
    )
    resolved_verification_provider = (
        resolveProviderForModelSelection(requested_verification_provider, requested_verification_model)
        if requested_verification_model
        else requested_verification_provider
    )
    resolved_verification_model = (
        normalizeModelName(requested_verification_model, resolved_verification_provider)
        if requested_verification_model and resolved_verification_provider
        else requested_verification_model
    )

    anthropic_headers = {
        **((auth or {}).get("customHeaders") or {}),
        **_parse_custom_headers(settings_env.get("ANTHROPIC_CUSTOM_HEADERS")),
        **_parse_custom_headers(os.environ.get("ANTHROPIC_CUSTOM_HEADERS")),
    }
    custom_headers = {
        **selected_profile.get("headers", {}),
        **(anthropic_headers if resolved_provider == "anthropic" else {}),
    }
    compaction_provider_profile = await normalizeCompactionProviderSecrets(
        resolveCompactionProviderProfileBinding(
            {
                "requestedProfileName": os.environ.get("TESS_COMPACTION_PROVIDER_PROFILE")
                or settings_env.get("TESS_COMPACTION_PROVIDER_PROFILE")
                or merged_settings.get("compactionProviderProfile"),
                "providerProfiles": provider_profiles,
                "settingsEnv": settings_env,
                "openaiStoredToken": openai_stored_token,
                "openaiEnvToken": openai_env_token,
                "openaiCodexToken": openai_codex_token,
                "explicitApiKey": explicit_api_key,
                "explicitAuthToken": explicit_auth_token,
                "storedAnthropicApiKey": stored_anthropic_api_key,
                "storedAnthropicAuthToken": stored_anthropic_auth_token,
                "anthropicHeaders": anthropic_headers,
            }
        ),
        f"config:{selected_name}:compaction",
    )
    compaction_verification_provider_profile = await normalizeCompactionProviderSecrets(
        resolveCompactionProviderProfileBinding(
            {
                "requestedProfileName": os.environ.get("TESS_COMPACTION_VERIFICATION_PROVIDER_PROFILE")
                or settings_env.get("TESS_COMPACTION_VERIFICATION_PROVIDER_PROFILE")
                or merged_settings.get("compactionVerificationProviderProfile"),
                "providerProfiles": provider_profiles,
                "settingsEnv": settings_env,
                "openaiStoredToken": openai_stored_token,
                "openaiEnvToken": openai_env_token,
                "openaiCodexToken": openai_codex_token,
                "explicitApiKey": explicit_api_key,
                "explicitAuthToken": explicit_auth_token,
                "storedAnthropicApiKey": stored_anthropic_api_key,
                "storedAnthropicAuthToken": stored_anthropic_auth_token,
                "anthropicHeaders": anthropic_headers,
            }
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
            "settingsEnv": settings_env,
        }
    )
    openai_token = _resolve_openai_credential(
        {
            "profile": selected_profile,
            "settingsEnv": settings_env,
            "storedToken": openai_stored_token,
            "envDefaultToken": openai_env_token,
            "codexToken": openai_codex_token,
        }
    )
    normalized_api_key = await normalizeRuntimeSecretValue(
        value=anthropic_creds.get("apiKey"),
        purpose="provider_api_key",
        idempotencyScope=f"config:{selected_name}:api-key",
    )
    normalized_auth_token = await normalizeRuntimeSecretValue(
        value=anthropic_creds.get("authToken"),
        purpose="provider_oauth_token",
        acceptedPurposes=("provider_oauth_token", "provider_setup_token"),
        idempotencyScope=f"config:{selected_name}:auth-token",
    )
    normalized_openai_token = await normalizeRuntimeSecretValue(
        value=openai_token,
        purpose="provider_oauth_token"
        if isinstance(openai_token, str) and openai_token == openai_codex_token
        else "provider_api_key",
        acceptedPurposes=("provider_api_key", "provider_oauth_token"),
        idempotencyScope=f"config:{selected_name}:openai-token",
    )
    normalized_custom_headers = await normalizeProviderHeaders(
        custom_headers,
        f"config:{selected_name}",
    )

    tools = _parse_tool_list(options.get("tools"), merged_settings.get("tools"), defaultToolList())
    allowed_tools = _dedupe(_parse_tool_list(options.get("allowedTools"), merged_settings.get("allowedTools"), tools))
    disallowed_tools = _dedupe(_parse_tool_list(options.get("disallowedTools"), merged_settings.get("disallowedTools"), []))
    plugin_dirs = [resolveMaybeRelative(cwd, item) for item in options.get("pluginDir", [])]
    allowed_roots = _dedupe([cwd, getPlansDir(), *[resolveMaybeRelative(cwd, item) for item in options.get("addDir", [])]])

    agents = {**clone(BUILTIN_AGENTS), **(merged_settings.get("agents") or {}), **(_parse_inline_agents(options.get("agents")) or {})}
    assistant_name = options.get("assistantName") or merged_settings.get("assistantName") or DEFAULT_ASSISTANT_NAME
    assistant_description = options.get("assistantDescription") or merged_settings.get("assistantDescription") or DEFAULT_ASSISTANT_DESCRIPTION
    system_prompt = (
        options.get("systemPrompt")
        or merged_settings.get("systemPrompt")
        or options.get("defaultSystemPrompt")
        or merged_settings.get("defaultSystemPrompt")
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
        "compactionProvider": resolved_compaction_provider,
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
        "compactionModel": resolved_compaction_model,
        "compactionVerificationProvider": resolved_verification_provider,
        "compactionVerificationModel": resolved_verification_model,
        "compactionVerificationProviderProfile": compaction_verification_provider_profile,
        "effort": normalizeEffortLevel(options.get("effort") or os.environ.get("TESS_EFFORT_LEVEL") or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL") or selected_profile.get("effortLevel") or merged_settings.get("effortLevel")) or getDefaultEffortForProvider(resolved_provider),
        "assistantName": assistant_name,
        "assistantDescription": assistant_description,
        "systemPrompt": system_prompt,
        "appendSystemPrompt": options.get("appendSystemPrompt") or merged_settings.get("appendSystemPrompt"),
        "sandbox": sandbox_config,
        "subagents": subagents_config,
        "webSearch": web_search_config,
        "settingsEnv": _omit_provider_credential_environment(settings_env, provider_profiles),
        "tools": tools,
        "allowedTools": allowed_tools,
        "disallowedTools": disallowed_tools,
        "inputFormat": options.get("inputFormat") or "text",
        "outputFormat": options.get("outputFormat") or "text",
        "permissionMode": options.get("permissionMode") or merged_settings.get("permissionMode") or "default",
        "screenMode": "inline"
        if options.get("noAltScreen") or options.get("altScreen") is False
        else (
            normalizeTuiScreenMode(
                options.get("screenMode")
                or os.environ.get("TESS_SCREEN_MODE")
                or os.environ.get("TESS_ALTERNATE_SCREEN")
                or settings_env.get("TESS_SCREEN_MODE")
                or settings_env.get("TESS_ALTERNATE_SCREEN")
                or merged_settings.get("screenMode")
            )
            or DEFAULT_TUI_SCREEN_MODE
        ),
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
        "permissionRules": await loadPermissionRules(
            cwd=cwd,
            sources=sources,
            inlineSettings=options.get("settings"),
        ),
        "hooks": await loadHooks(cwd=cwd, sources=sources),
        "mcpConfig": await _load_mcp_config(cwd, options.get("mcpConfig"), sources, strict_mcp),
        "mcpConfigPolicy": {"mode": "explicit_only" if strict_mcp else "merged"},
        "channels": _dedupe(options.get("channels") or []),
        "developmentChannels": _dedupe(options.get("dangerouslyLoadDevelopmentChannels") or []),
        "pluginDirs": plugin_dirs,
        "worktree": options.get("worktree"),
        "name": options.get("name"),
    }

resolve_config = resolveConfig
