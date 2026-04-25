from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from ._utils import clone
from .providers.openai import isCodexBaseUrl
from .storage import (
    configureRuntimeHome,
    ensureBaseLayout,
    fileOrJson,
    getPlansDir,
    loadAuth,
    loadCodexAuthToken,
    loadMcpServers,
    loadSettingsBySources,
    resolveMaybeRelative,
    resolveRuntimeHome,
)


DEFAULT_ASSISTANT_NAME = "assistant"
DEFAULT_ASSISTANT_DESCRIPTION = "a helpful and reliable software engineering agent"
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_EFFORT = "xhigh"
DEFAULT_OPENAI_API_MODE = "responses"
DEFAULT_OPENAI_CODEX_AUTH_PATH = os.path.join(os.environ.get("CODEX_HOME", "~/.codex"), "auth.json")
DEFAULT_PROVIDER_PROFILE_NAME = "openai"
DEFAULT_TUI_SCREEN_MODE = "auto"

VALID_EFFORT_LEVELS = ["low", "medium", "high", "max", "xhigh"]

PROVIDER_MODELS = {
    "anthropic": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "openai": ["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"],
}
PROVIDER_DEFAULT_MODEL = {"anthropic": DEFAULT_MODEL, "openai": "gpt-5.4"}
PROVIDER_DEFAULT_EFFORT = {"anthropic": "high", "openai": "xhigh"}

BUILTIN_PROVIDER_PROFILES: dict[str, dict[str, Any]] = {
    "openai": {
        "kind": "openai",
        "apiMode": "responses",
        "defaultModel": "gpt-5.4",
        "auth": {"kind": "openai_api_key", "envVar": "OPENAI_API_KEY"},
        "description": "Official OpenAI Responses API",
    },
    "codex": {
        "kind": "openai",
        "apiMode": "codex",
        "defaultModel": "gpt-5.4",
        "auth": {"kind": "openai_api_key_or_codex", "envVar": "OPENAI_API_KEY"},
        "description": "OpenAI Codex-compatible backend",
    },
    "anthropic-console": {
        "kind": "anthropic",
        "defaultModel": "claude-sonnet-4-6",
        "auth": {"kind": "anthropic_api_key", "envVar": "ANTHROPIC_API_KEY"},
        "description": "Anthropic Console API key",
    },
    "anthropic-claude-code": {
        "kind": "anthropic",
        "defaultModel": "claude-sonnet-4-6",
        "auth": {"kind": "anthropic_auth_token", "envVar": "ANTHROPIC_AUTH_TOKEN"},
        "description": "Claude Code / Claude.ai token",
    },
}

BUILTIN_AGENTS = {
    "default": {
        "description": "General-purpose coding agent",
        "prompt": "You are a pragmatic software engineer. Solve the user's task end to end, inspect the codebase before changing it, and verify your work.",
    },
    "reviewer": {
        "description": "Code reviewer",
        "prompt": "You are a strict code reviewer. Prioritize bugs, regressions, risky assumptions, and missing tests. Keep summaries brief.",
    },
    "planner": {
        "description": "Planning agent",
        "prompt": "You are a planning-focused engineering lead. Break work into concrete steps, note assumptions, and emphasize execution order.",
    },
    "debugger": {
        "description": "Debugging agent",
        "prompt": "You are a debugging specialist. Reproduce issues, isolate causes, propose the smallest defensible fix, and verify the result.",
    },
}


def _opt(options: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    if options is None:
        return default
    return options.get(key, default)


def _looks_like_claude_ai_token(value: str | None) -> bool:
    return bool(value and re.match(r"^sk-ant-o", value, re.I))


async def resolveConfig(options: dict[str, Any] | None, cwd: str) -> dict[str, Any]:
    options = options or {}
    runtime_home = resolveRuntimeHome(cwd, options.get("runtimeHome"))
    configureRuntimeHome(runtime_home)
    await ensureBaseLayout(cwd)

    sources = _parse_setting_sources(options.get("settingSources"))
    merged_settings = await _load_merged_settings(sources, cwd, options.get("settings"))
    auth = await loadAuth()
    settings_env = merged_settings.get("env") or {}
    helper_auth_value = await _resolve_api_key_from_helper(merged_settings)
    provider_profiles = resolveProviderProfiles(merged_settings.get("providerProfiles"))

    requested_provider_name = options.get("provider") or os.environ.get("TESS_PROVIDER") or merged_settings.get("provider")
    direct_model_input = options.get("model") or merged_settings.get("model") or os.environ.get("TESS_MODEL")
    explicit_provider_kind = normalizeProviderName(requested_provider_name)
    requested_model_input = direct_model_input or (
        os.environ.get("OPENAI_MODEL")
        if explicit_provider_kind == "openai"
        else os.environ.get("ANTHROPIC_MODEL")
        if explicit_provider_kind == "anthropic"
        else os.environ.get("ANTHROPIC_MODEL") or os.environ.get("OPENAI_MODEL")
    )

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
    openai_api_mode = selected_profile.get("apiMode") if resolved_provider == "openai" else DEFAULT_OPENAI_API_MODE
    resolved_model = normalizeModelName(
        requested_model_input or selected_profile.get("defaultModel") or getDefaultModelForProvider(resolved_provider),
        resolved_provider,
    )

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

    tools = _parse_tool_list(options.get("tools"), merged_settings.get("tools"), defaultToolList())
    allowed_tools = _dedupe(_parse_tool_list(options.get("allowedTools"), merged_settings.get("allowedTools"), tools))
    disallowed_tools = _dedupe(_parse_tool_list(options.get("disallowedTools"), merged_settings.get("disallowedTools"), []))
    plugin_dirs = [resolveMaybeRelative(cwd, item) for item in options.get("pluginDir", [])]
    allowed_roots = _dedupe([cwd, getPlansDir(), *[resolveMaybeRelative(cwd, item) for item in options.get("addDir", [])]])

    agents = {**clone(BUILTIN_AGENTS), **(merged_settings.get("agents") or {}), **(_parse_inline_agents(options.get("agents")) or {})}
    assistant_name = options.get("assistantName") or merged_settings.get("assistantName") or DEFAULT_ASSISTANT_NAME
    assistant_description = options.get("assistantDescription") or merged_settings.get("assistantDescription") or DEFAULT_ASSISTANT_DESCRIPTION
    system_prompt = options.get("systemPrompt") or merged_settings.get("systemPrompt") or buildDefaultSystemPrompt({"assistantName": assistant_name, "assistantDescription": assistant_description})

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
        "providerProfiles": {name: resolveProviderProfile(name, record) for name, record in provider_profiles.items()},
        "compactionProvider": resolved_compaction_provider,
        "apiKey": anthropic_creds.get("apiKey"),
        "authToken": anthropic_creds.get("authToken"),
        "customHeaders": custom_headers,
        "anthropicBaseUrl": selected_profile.get("baseUrl") if resolved_provider == "anthropic" else None,
        "openaiToken": openai_token,
        "openaiApiMode": openai_api_mode,
        "openaiBaseUrl": selected_profile.get("baseUrl") if resolved_provider == "openai" else None,
        "openaiCodexAuthPath": openai_codex_auth_path,
        "model": resolved_model,
        "compactionModel": resolved_compaction_model,
        "compactionVerificationProvider": resolved_verification_provider,
        "compactionVerificationModel": resolved_verification_model,
        "effort": normalizeEffortLevel(options.get("effort") or os.environ.get("TESS_EFFORT_LEVEL") or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL") or merged_settings.get("effortLevel")) or getDefaultEffortForProvider(resolved_provider),
        "assistantName": assistant_name,
        "assistantDescription": assistant_description,
        "systemPrompt": system_prompt,
        "appendSystemPrompt": options.get("appendSystemPrompt") or merged_settings.get("appendSystemPrompt"),
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
        "mcpConfig": await _load_mcp_config(cwd, options.get("mcpConfig")),
        "channels": _dedupe(options.get("channels") or []),
        "developmentChannels": _dedupe(options.get("dangerouslyLoadDevelopmentChannels") or []),
        "pluginDirs": plugin_dirs,
        "worktree": options.get("worktree"),
        "name": options.get("name"),
    }


resolve_config = resolveConfig


def getDefaultSystemPrompt() -> str:
    return buildDefaultSystemPrompt()


def getDefaultAssistantName() -> str:
    return DEFAULT_ASSISTANT_NAME


def getDefaultAssistantDescription() -> str:
    return DEFAULT_ASSISTANT_DESCRIPTION


def getDefaultModel() -> str:
    return DEFAULT_MODEL


def getDefaultProvider() -> str:
    return DEFAULT_PROVIDER


def getDefaultModelForProvider(provider: str) -> str:
    return PROVIDER_DEFAULT_MODEL[provider]


def listProviders() -> list[str]:
    return ["anthropic", "openai"]


def listProviderProfiles(profiles: dict[str, Any] | None = None) -> list[str]:
    return sorted((profiles or BUILTIN_PROVIDER_PROFILES).keys())


def listProviderProfilesForKind(kind: str, profiles: dict[str, Any] | None = None) -> list[str]:
    profiles = profiles or BUILTIN_PROVIDER_PROFILES
    return [name for name in listProviderProfiles(profiles) if profiles.get(name, {}).get("kind") == kind]


def listModelsForProvider(provider: str) -> list[str]:
    return PROVIDER_MODELS[provider]


def getDefaultEffort() -> str:
    return DEFAULT_EFFORT


def getDefaultEffortForProvider(provider: str) -> str:
    return PROVIDER_DEFAULT_EFFORT[provider]


def normalizeEffortLevel(input: str | None) -> str | None:
    normalized = input.strip().lower() if isinstance(input, str) else ""
    return normalized if normalized in VALID_EFFORT_LEVELS else None


def listEffortLevels() -> list[str]:
    return list(VALID_EFFORT_LEVELS)


def normalizeProviderName(input: str | None) -> str | None:
    normalized = input.strip().lower() if isinstance(input, str) else ""
    if normalized in ("anthropic", "claude"):
        return "anthropic"
    if normalized in ("openai", "codex", "openai-codex"):
        return "openai"
    return None


def normalizeProviderProfileName(input: str | None) -> str | None:
    normalized = input.strip().lower() if isinstance(input, str) else ""
    return normalized or None


def normalizeOpenAiApiModeSetting(input: str | None) -> str | None:
    normalized = input.strip().lower() if isinstance(input, str) else ""
    if not normalized:
        return None
    if normalized == "auto":
        return "auto"
    if normalized in ("codex", "openai-codex"):
        return "codex"
    if normalized in ("responses", "response", "openai-responses"):
        return "responses"
    if normalized in ("chat", "chat-completions", "chat_completions", "chatcompletions", "openai-chat"):
        return "chat_completions"
    return None


def normalizeTuiScreenMode(input: str | None) -> str | None:
    normalized = input.strip().lower() if isinstance(input, str) else ""
    if normalized == "auto":
        return "auto"
    if normalized in ("fullscreen", "full-screen", "full_screen", "bounded"):
        return "fullscreen"
    if normalized in ("inline", "scrollback", "no-alt-screen", "no_alt_screen"):
        return "inline"
    return None


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
    return {
        "name": name,
        "kind": record["kind"],
        "apiMode": api_mode,
        "baseUrl": (overrides.get("openaiBaseUrl") if record.get("kind") == "openai" else None) or (record.get("baseUrl") or None),
        "defaultModel": record.get("defaultModel"),
        "auth": clone(record.get("auth") or {}),
        "headers": clone(record.get("headers") or {}),
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
        return "opus-4-6" in normalized or "sonnet-4-6" in normalized
    return normalized.startswith("gpt-5.4") or normalized.startswith("gpt-5")


def modelSupportsMaxEffort(provider: str, model: str) -> bool:
    return provider == "anthropic" and "opus-4-6" in model.strip().lower()


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


def resolveAgentDefinition(config: dict[str, Any], agentName: str | None) -> dict[str, Any] | None:
    return None if not agentName else config.get("agents", {}).get(agentName)


def buildEffectiveSystemPrompt(config: dict[str, Any]) -> str:
    if not config.get("appendSystemPrompt"):
        return config["systemPrompt"]
    return f"{config['systemPrompt']}\n\n{config['appendSystemPrompt']}"


def buildDefaultSystemPrompt(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    assistant_name = (args.get("assistantName") or DEFAULT_ASSISTANT_NAME).strip()
    assistant_description = (args.get("assistantDescription") or DEFAULT_ASSISTANT_DESCRIPTION).strip()
    return "\n".join(
        [
            f"You are {assistant_name}, {assistant_description}.",
            "Work directly in the user's codebase.",
            "Prefer concrete actions over long explanations.",
            "Use tools when needed, keep edits minimal, and preserve user intent.",
            "Before modifying files, inspect the relevant code and respect the existing style.",
            "Use SetStatus to keep the live UI updated when you change phases or begin a meaningful chunk of work. Keep statuses brief and concrete, for example: 'Reading plan...', 'Searching code...', 'Updating plan...', or 'Running tests...'.",
        ]
    )


def buildSystemPromptForAgent(config: dict[str, Any], agentName: str | None, fallbackSystemPrompt: str | None = None) -> str:
    fallback = fallbackSystemPrompt or buildEffectiveSystemPrompt(config)
    definition = resolveAgentDefinition(config, agentName)
    if not definition or not definition.get("prompt"):
        return fallback
    return f"{fallback}\n\n# Agent Instructions\n{definition['prompt']}"


def resolveModelForAgent(config: dict[str, Any], agentName: str | None, fallbackModel: str | None = None) -> str:
    definition = resolveAgentDefinition(config, agentName)
    model = (definition or {}).get("model") or fallbackModel or config["model"]
    return normalizeModelName(model, resolveProviderForModelSelection(config.get("provider"), model))


def buildPlanModeSystemPrompt(basePrompt: str, args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    state = args.get("currentState") or "inactive"
    prior_plan = (args.get("priorPlan") or "").strip()
    overlay = ["# Plan Mode", "You are in plan mode.", "Do not implement the change yet."]
    if state == "chatting":
        overlay.extend(
            [
                "There is an existing draft plan under discussion.",
                "Answer the user's follow-up questions directly and conversationally while staying in plan mode.",
                "Do not start implementation while plan mode is still active.",
                "Do not use implementation tools, create implementation todos, or describe implementation as already underway until plan mode has been exited.",
                "If the user asks you to proceed, go ahead, use a specific document as the implementation reference, or otherwise indicates the planning discussion is complete, treat that as a request to update/finalize the plan and then call ExitPlanMode.",
                "If the user explicitly asks for a plan revision, update the plan in structured markdown and then call ExitPlanMode.",
            ]
        )
    else:
        overlay.extend(
            [
                "Investigate the user's request and produce a concrete implementation plan before any implementation work begins.",
                "Use read-only investigation and subagents as needed to understand the codebase and scope of work.",
                "Respond with a structured markdown plan that includes:",
                "1. Objective",
                "2. Findings",
                "3. Proposed changes",
                "4. Ordered implementation steps",
                "5. Validation strategy",
                "6. Risks or open questions",
                "When the plan is ready, call ExitPlanMode with allowedPrompts describing the categories of Bash actions expected during implementation.",
                "Do not ask for plan approval in plain text; use ExitPlanMode.",
            ]
        )
    if state in ("chatting", "rejected") and prior_plan:
        overlay.extend(["", "The previous plan was rejected. Revise it and submit an improved plan." if state == "rejected" else "This is the current draft plan being discussed.", "", "## Current Draft Plan", prior_plan])
    return f"{basePrompt}\n\n" + "\n".join(overlay)


def isPlanRevisionPrompt(prompt: str) -> bool:
    normalized = prompt.strip().lower()
    if not normalized:
        return False
    patterns = [
        r"\bupdate (the )?plan\b",
        r"\brevise (the )?plan\b",
        r"\bmodify (the )?plan\b",
        r"\bregenerate (the )?plan\b",
        r"\brefresh (the )?plan\b",
        r"\bnew plan\b",
        r"\bfinali[sz]e (the )?plan\b",
        r"\bshow (me )?(the )?updated plan\b",
        r"\bproceed with implementation\b",
        r"\bgo ahead and (build|implement|start)\b",
        r"\bstart (building|implementing)\b",
        r"\buse the original (plan|document|doc)\b",
        r"\buse .* as (the )?(spec|reference|implementation guide)\b",
    ]
    return any(re.search(pattern, normalized) for pattern in patterns)


def filterToolsByPolicy(config: dict[str, Any]) -> list[str]:
    allow = set(config.get("allowedTools") or [])
    deny = set(config.get("disallowedTools") or [])
    return [tool for tool in config.get("tools", []) if tool not in deny and (not allow or tool in allow)]


def getDefaultToolList() -> list[str]:
    return defaultToolList()


def defaultToolList() -> list[str]:
    return [
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "TodoWrite",
        "WebFetch",
        "WebSearch",
        "SetStatus",
        "Agent",
        "AskUserQuestion",
        "Config",
        "ExitPlanMode",
        "NotebookEdit",
        "TaskOutput",
        "TaskStop",
        "TaskMonitorStart",
        "TaskMonitorStop",
        "Mcp",
        "ListMcpResources",
        "ReadMcpResource",
        "EnterWorktree",
        "ExitWorktree",
    ]


def _parse_setting_sources(raw: str | None) -> list[str]:
    parts = [part.strip() for part in raw.split(",")] if raw else ["user", "project", "local"]
    valid = [part for part in parts if part in ("user", "project", "local")]
    return valid or ["user", "project", "local"]


async def _load_merged_settings(sources: list[str], cwd: str, inline_settings: str | None) -> dict[str, Any]:
    merged = await loadSettingsBySources(sources, cwd)  # type: ignore[arg-type]
    direct = fileOrJson(inline_settings) or {}
    return {**merged, **direct, "providerProfiles": {**(merged.get("providerProfiles") or {}), **(direct.get("providerProfiles") or {})}}


async def _resolve_api_key_from_helper(settings: dict[str, Any]) -> str | None:
    helper = settings.get("apiKeyHelper")
    if not helper:
        return None
    parts = str(helper).split()
    if not parts:
        return None
    try:
        result = subprocess.run(parts, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        return result.stdout.strip() or None
    except Exception:
        return None


def _parse_custom_headers(raw: str | None) -> dict[str, str]:
    if not raw or not raw.strip():
        return {}
    trimmed = raw.strip()
    if trimmed.startswith("{"):
        parsed = json.loads(trimmed)
        return {str(k): v for k, v in parsed.items() if isinstance(v, str)}
    headers = {}
    for line in re.split(r"\r?\n|;", trimmed):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def _parse_tool_list(cli_value: list[str] | None, settings_value: list[str] | None, fallback: list[str]) -> list[str]:
    source = cli_value if cli_value is not None else settings_value if settings_value is not None else fallback
    if len(source) == 1 and source[0] == "":
        return []
    parsed: list[str] = []
    for entry in source:
        parsed.extend(item.strip() for item in re.split(r"[,\s]+", entry) if item.strip())
    if parsed == ["default"]:
        return fallback
    return _dedupe([_strip_tool_pattern(item) for item in parsed])


def _strip_tool_pattern(value: str) -> str:
    match = re.match(r"^([A-Za-z]+)(?:\(.+\))?$", value)
    return match.group(1) if match else value


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _parse_inline_agents(raw: str | None) -> dict[str, Any] | None:
    return json.loads(raw) if raw else None


def _parse_json_schema(raw: str | None) -> Any:
    return json.loads(raw) if raw else None


async def _load_mcp_config(cwd: str, inputs: list[str] | None) -> list[dict[str, Any]]:
    existing = []
    for scope in ("user", "project", "local"):
        existing.extend(await loadMcpServers(scope, cwd))  # type: ignore[arg-type]
    inline = []
    for item in inputs or []:
        parsed = fileOrJson(item)
        if isinstance(parsed, list):
            inline.extend(parsed)
        elif parsed:
            inline.append(parsed)
    return [{**item, "commandOrUrl": os.path.expanduser(item.get("commandOrUrl", ""))} for item in [*existing, *inline]]


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
        "description": (record.get("description") or "").strip() or None,
    }


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
