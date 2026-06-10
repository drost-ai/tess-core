"""Resolved config and settings contracts.

Producers: ``agent_core/config_store/resolution.py`` (``resolveConfig``),
``agent_core/config_store/shared.py`` (builtin agents and provider profiles),
``agent_core/storage_store/settings.py`` (persisted settings records).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


# Mirrors PERMISSION_MODES in tools/permissions.py (sync-tested).
PermissionMode = Literal["default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"]

ProviderKind = Literal["anthropic", "openai"]

OpenAiApiMode = Literal["chat_completions", "responses", "codex"]


class AgentDefinition(TypedDict, total=False):
    """Agent definition shape in BUILTIN_AGENTS / settings ``agents`` maps."""

    description: str
    prompt: str
    model: str | None
    tools: list[str]


class AutoModePolicy(TypedDict, total=False):
    """Auto-mode policy record (storage_store/settings.py DEFAULT_AUTO_MODE)."""

    allow: list[str]
    deny: list[str]
    environment: dict[str, str]


class ProviderProfile(TypedDict, total=False):
    """Provider profile shape.

    Builtin profiles (config_store/shared.py BUILTIN_PROVIDER_PROFILES) carry
    the kind/apiMode/defaultModel/auth/description subset; the resolved and
    normalized profile (config_store/providers.py resolveProviderProfile)
    additionally always carries name/headers/supportsVision/capabilities/
    effortLevel/requestOptions/limits.
    """

    kind: ProviderKind
    name: str
    description: str | None
    defaultModel: str | None
    apiMode: OpenAiApiMode | None
    auth: dict[str, Any]
    baseUrl: str | None
    headers: dict[str, str]
    effortLevel: str | None
    supportsVision: bool | None
    capabilities: dict[str, Any]
    requestOptions: dict[str, Any]
    limits: dict[str, Any]


class SettingsRecord(TypedDict, total=False):
    """Persisted settings.json shape (storage_store/settings.py). Sparse file."""

    provider: str
    model: str
    effort: str
    permissionMode: PermissionMode
    assistantName: str
    assistantDescription: str
    systemPrompt: str
    appendSystemPrompt: str
    tools: list[str]
    allowedTools: list[str]
    disallowedTools: list[str]
    agents: dict[str, AgentDefinition]
    providerProfiles: dict[str, ProviderProfile]
    env: dict[str, str]
    compactionProvider: str
    compactionModel: str
    compactionVerificationProvider: str
    compactionVerificationModel: str
    screenMode: str
    openaiApiMode: OpenAiApiMode
    openaiBaseUrl: str
    openaiCodexAuthPath: str


class _ResolvedConfigRequired(TypedDict):
    cwd: str
    runtimeHome: str
    allowedRoots: list[str]
    provider: ProviderKind
    providerKind: ProviderKind
    model: str
    systemPrompt: str
    tools: list[str]
    permissionMode: PermissionMode


class ResolvedConfig(_ResolvedConfigRequired, total=False):
    """Output of resolveConfig (config_store/resolution.py).

    All keys below are emitted by resolveConfig today; they are optional here
    because callers construct partial config dicts in tests and subagent
    preparation paths.
    """

    agent: str | None
    agents: dict[str, AgentDefinition]
    providerProfileName: str
    providerProfile: ProviderProfile
    providerProfiles: dict[str, ProviderProfile]
    compactionProvider: str | None
    compactionModel: str | None
    compactionVerificationProvider: str | None
    compactionVerificationModel: str | None
    apiKey: str | None
    authToken: str | None
    customHeaders: dict[str, str]
    anthropicBaseUrl: str | None
    openaiToken: str | None
    openaiApiMode: OpenAiApiMode
    openaiBaseUrl: str | None
    openaiCodexAuthPath: str
    effort: str
    assistantName: str
    assistantDescription: str
    appendSystemPrompt: str | None
    allowedTools: list[str]
    disallowedTools: list[str]
    inputFormat: str
    outputFormat: str
    screenMode: str
    sessionPersistence: bool
    jsonSchema: dict[str, Any] | None
    maxBudgetUsd: float | None
    fallbackModel: str | None
    debugEnabled: bool
    debugFilter: str | None
    verbose: bool
    bare: bool
    settingsSources: list[str]
    mcpConfig: list[dict[str, Any]]
    channels: list[str]
    developmentChannels: list[str]
    pluginDirs: list[str]
    worktree: str | None
    name: str | None
