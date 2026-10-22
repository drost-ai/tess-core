"""Resolved config and settings contracts.

Producers: ``agent_core/config/resolution.py`` (``resolve_config``),
``agent_core/config/shared.py`` (builtin agents and provider profiles),
``agent_core/storage/settings.py`` (persisted settings records).
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict, Union, get_args


PermissionMode = Literal["default", "acceptEdits", "bypassPermissions", "dontAsk", "auto"]

# The one runtime value set for PermissionMode: deriving it from the Literal
# means the type and the set cannot drift, and every former copy imports it.
PERMISSION_MODES: frozenset[str] = frozenset(get_args(PermissionMode))

ProviderKind = Literal["anthropic", "openai"]

OpenAiApiMode = Literal["chat_completions", "responses", "codex"]

RuntimeSecretPurpose = Literal[
    "mcp_header",
    "mcp_oauth",
    "mcp_env",
    "provider_api_key",
    "provider_oauth_token",
    "provider_setup_token",
]


class RuntimeSecretRef(TypedDict):
    kind: Literal["runtime_secret"]
    id: str
    purpose: RuntimeSecretPurpose
    version: Literal[1]
    backend: NotRequired[Literal["macos_keychain", "linux_secret_service"]]
    installation_id: NotRequired[str]
    account_id: NotRequired[str]


RuntimeSecretValue = Union[str, RuntimeSecretRef]


class AgentDefinition(TypedDict, total=False):
    """Agent definition shape in BUILTIN_AGENTS / settings ``agents`` maps."""

    description: str
    prompt: str
    model: str | None
    tools: list[str]


class AutoModePolicy(TypedDict, total=False):
    """Auto-mode policy record (storage/settings.py DEFAULT_AUTO_MODE)."""

    allow: list[str]
    deny: list[str]
    environment: dict[str, str]


class WebSearchSettings(TypedDict, total=False):
    """Non-secret web search settings for the built-in WebSearch tool."""

    provider: str
    exaApiKeyEnv: str
    urlAllowlist: list[str]


class ProviderProfile(TypedDict, total=False):
    """Provider profile shape.

    Builtin profiles (config/shared.py BUILTIN_PROVIDER_PROFILES) carry
    the kind/apiMode/defaultModel/auth/description subset; the resolved and
    normalized profile (config/providers.py resolve_provider_profile)
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
    headers: dict[str, RuntimeSecretValue]
    effortLevel: str | None
    supportsVision: bool | None
    payloadClass: str | None
    capabilities: dict[str, Any]
    maxContextLength: int
    maxOutputTokens: int
    compactionThresholdTokens: int
    compactionTargetTokens: int
    compactionOutputTokens: int
    contextReliefThresholdTokens: int | None
    contextReliefTargetTokens: int | None
    inlineToolResultBatchBytes: int | None
    inlineToolResultTurnBytes: int | None
    reliefSource: str
    toolBudgetSource: str
    deprecatedTurnBudgetIgnored: bool
    reasoningEffortEnabled: bool | None
    reasoningEffort: str | None
    openAiChatReasoning: dict[str, Any]
    openAiChatReasoningSummary: str | None
    anthropicThinkingDisplay: str | None
    chatTemplateKwargs: dict[str, Any]
    requestStartTimeoutMs: int | None
    streamIdleTimeoutMs: int | None
    tokenCountTimeoutMs: int | None
    totalRequestTimeoutMs: int | None
    exactTokenCountRequired: bool
    requestOptions: dict[str, Any]
    limits: dict[str, Any]


class DurableMemorySettings(TypedDict, total=False):
    enabled: bool


class SettingsRecord(TypedDict, total=False):
    """Persisted settings.json shape (storage/settings.py). Sparse file."""

    provider: str
    model: str
    effort: str
    permissionMode: PermissionMode
    assistantName: str
    assistantDescription: str
    durableMemory: DurableMemorySettings
    systemPromptFile: str
    systemPromptAppendFile: str
    tools: list[str]
    allowedTools: list[str]
    disallowedTools: list[str]
    agents: dict[str, AgentDefinition]
    providerProfiles: dict[str, ProviderProfile]
    env: dict[str, str]
    compactionProvider: str
    compactionModel: str
    compactionEffort: str
    compactionProviderProfile: str
    compactionVerificationProvider: str
    compactionVerificationModel: str
    compactionVerificationEffort: str
    compactionVerificationProviderProfile: str
    screenMode: str
    openaiApiMode: OpenAiApiMode
    openaiBaseUrl: str
    openaiCodexAuthPath: str
    sandbox: dict[str, Any]
    subagents: dict[str, Any]
    webSearch: WebSearchSettings
    permissions: dict[str, list[str]]
    hooks: dict[str, list[dict[str, Any]]]


class _resolved_config_required(TypedDict):
    cwd: str
    runtimeHome: str
    allowedRoots: list[str]
    provider: ProviderKind
    providerKind: ProviderKind
    model: str
    systemPrompt: str
    tools: list[str]
    permissionMode: PermissionMode


class ResolvedConfig(_resolved_config_required, total=False):
    """Output of resolve_config (config/resolution.py).

    All keys below are emitted by resolve_config today; they are optional here
    because callers construct partial config dicts in tests and subagent
    preparation paths.
    """

    agent: str | None
    agents: dict[str, AgentDefinition]
    providerRegistry: Any  # Explicit ProviderRegistry; None retains legacy resolution.
    providerAuthPath: str | None
    providerProfileName: str
    providerProfile: ProviderProfile
    providerCapabilities: dict[str, Any]
    providerProfiles: dict[str, ProviderProfile]
    compactionProvider: str | None
    compactionModel: str | None
    compactionEffort: str | None
    compactionProviderProfile: dict[str, Any] | None
    compactionVerificationProvider: str | None
    compactionVerificationModel: str | None
    compactionVerificationEffort: str | None
    compactionVerificationProviderProfile: dict[str, Any] | None
    apiKey: RuntimeSecretValue | None
    authToken: RuntimeSecretValue | None
    customHeaders: dict[str, RuntimeSecretValue]
    anthropicBaseUrl: str | None
    openaiToken: RuntimeSecretValue | None
    openaiApiMode: OpenAiApiMode
    openaiBaseUrl: str | None
    openaiCodexAuthPath: str
    effort: str
    assistantName: str
    assistantDescription: str
    durableMemory: DurableMemorySettings
    appendSystemPrompt: str | None
    sandbox: dict[str, Any]
    subagents: dict[str, Any]
    webSearch: WebSearchSettings
    settings_env: dict[str, str]
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
    inlineSettings: str | None
    mcpConfig: list[dict[str, Any]]
    mcpConfigPolicy: dict[str, str]
    channels: list[str]
    developmentChannels: list[str]
    pluginDirs: list[str]
    worktree: str | None
    name: str | None
