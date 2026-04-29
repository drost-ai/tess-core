from __future__ import annotations

from .shared import *

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
