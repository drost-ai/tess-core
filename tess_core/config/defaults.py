from __future__ import annotations

from typing import Any

from .shared import (
    BUILTIN_PROVIDER_PROFILES,
    DEFAULT_ASSISTANT_DESCRIPTION,
    DEFAULT_ASSISTANT_NAME,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    PROVIDER_DEFAULT_EFFORT,
    PROVIDER_DEFAULT_MODEL,
    PROVIDER_MODELS,
    VALID_EFFORT_LEVELS,
)

def get_default_assistant_name() -> str:
    return DEFAULT_ASSISTANT_NAME

def get_default_assistant_description() -> str:
    return DEFAULT_ASSISTANT_DESCRIPTION

def get_default_model() -> str:
    return DEFAULT_MODEL

def get_default_provider() -> str:
    return DEFAULT_PROVIDER

def get_default_model_for_provider(provider: str) -> str:
    return PROVIDER_DEFAULT_MODEL[provider]

def list_providers() -> list[str]:
    return ["anthropic", "openai"]

def list_provider_profiles(profiles: dict[str, Any] | None = None) -> list[str]:
    return sorted((profiles or BUILTIN_PROVIDER_PROFILES).keys())

def list_provider_profiles_for_kind(kind: str, profiles: dict[str, Any] | None = None) -> list[str]:
    profiles = profiles or BUILTIN_PROVIDER_PROFILES
    return [name for name in list_provider_profiles(profiles) if profiles.get(name, {}).get("kind") == kind]

def list_models_for_provider(provider: str) -> list[str]:
    return PROVIDER_MODELS[provider]

def get_default_effort() -> str:
    return DEFAULT_EFFORT

def get_default_effort_for_provider(provider: str) -> str:
    return PROVIDER_DEFAULT_EFFORT[provider]

def normalize_effort_level(input: str | None) -> str | None:
    normalized = input.strip().lower() if isinstance(input, str) else ""
    return normalized if normalized in VALID_EFFORT_LEVELS else None

def list_effort_levels() -> list[str]:
    return list(VALID_EFFORT_LEVELS)

def normalize_provider_name(input: str | None) -> str | None:
    normalized = input.strip().lower() if isinstance(input, str) else ""
    if normalized in ("anthropic", "claude"):
        return "anthropic"
    if normalized in ("openai", "codex", "openai-codex"):
        return "openai"
    return None

def normalize_provider_profile_name(input: str | None) -> str | None:
    normalized = input.strip().lower() if isinstance(input, str) else ""
    return normalized or None

def normalize_openai_api_mode_setting(input: str | None) -> str | None:
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

def normalize_tui_screen_mode(input: str | None) -> str | None:
    normalized = input.strip().lower() if isinstance(input, str) else ""
    if normalized == "auto":
        return "auto"
    if normalized in ("fullscreen", "full-screen", "full_screen", "bounded"):
        return "fullscreen"
    if normalized in ("inline", "scrollback", "no-alt-screen", "no_alt_screen"):
        return "inline"
    return None
