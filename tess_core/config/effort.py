"""Which reasoning effort a request carries.

An effort that a profile or a session names is sent to the model, whatever the model is
called. When nobody names one, a model gets the provider's default only if its name is one
the runtime knows, and nothing is sent to a service other than the provider's own.
"""
from __future__ import annotations

import os
import re
from typing import Any

from ..providers.google.thinking import google_thinking_levels, resolve_google_thinking_level
from ..providers.openai import is_codex_base_url, supports_openai_chat_completions_reasoning_effort
from .defaults import get_default_effort_for_provider, normalize_effort_level


def resolve_session_effort(options: dict[str, Any], profile: dict[str, Any], settings: dict[str, Any], provider: str) -> dict[str, Any]:
    """A session's effort, and whether somebody named it.

    The named effort is the most specific of the session's, the environment's, the
    profile's and the settings'. When nobody named one the provider's default applies,
    and ``effortNamed`` is false: a default is sent only to a model the runtime knows."""
    named = normalize_effort_level(
        options.get("effort") or os.environ.get("TESS_EFFORT_LEVEL") or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL")
        or profile.get("effortLevel") or settings.get("effortLevel")
    )
    return {"effort": named or get_default_effort_for_provider(provider), "effortNamed": bool(named)}


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

def is_openai_compatible_service(profile: dict[str, Any] | None) -> bool:
    """Whether an OpenAI-kind profile reaches a service other than OpenAI's own: another
    company's, or a server of the owner's. OpenAI's API and the Codex backend are not."""
    if not isinstance(profile, dict) or profile.get("kind") != "openai" or profile.get("apiMode") == "codex":
        return False
    base_url = profile.get("baseUrl")
    if not isinstance(base_url, str) or not base_url.strip() or is_codex_base_url(base_url):
        return False
    return not supports_openai_chat_completions_reasoning_effort(base_url)

def profile_declares_effort(profile: dict[str, Any] | None, *, named: bool = False) -> bool | None:
    """What a profile says about reasoning effort, whatever its model is called.

    True when it names an effort (``reasoningEffort``), switches effort on
    (``reasoningEffortEnabled: true``), or the session named one (``named``). False when it
    switches effort off, which nothing overrides. None when nothing was said, and the
    model's name decides as it always did."""
    record = profile if isinstance(profile, dict) else {}
    enabled = record.get("reasoningEffortEnabled")
    if enabled is False:
        return False
    if enabled is True or named or record.get("effortLevel") or record.get("reasoningEffort"):
        return True
    return None

def resolve_request_effort(config: dict[str, Any], model: str, effort: str | None) -> str | None:
    """The effort a session's request carries on the profile its configuration resolved."""
    profile = config.get("providerProfile")
    return normalize_effort_for_model(
        config["provider"], model, effort,
        declared=profile_declares_effort(profile, named=bool(config.get("effortNamed"))),
        compatible=is_openai_compatible_service(profile),
    )

def normalize_effort_for_model(provider: str, model: str, effort: str | None, *, declared: bool | None = None, compatible: bool = False) -> str | None:
    """The effort to ask a model for, or None to ask for none.

    ``declared`` is what the profile says (``profile_declares_effort``) and ``compatible``
    whether it reaches a service other than OpenAI's (``is_openai_compatible_service``).
    Both concern OpenAI-kind profiles only. Without them a model is asked for an effort
    only when its name is one the runtime knows."""
    if provider == "openai":
        if not effort or declared is False:
            return None
        if compatible:
            # Another service takes its own values, so a named effort is sent as it was
            # named, and a default is never sent to it unasked.
            return effort if declared else None
        known = model_supports_effort(provider, model)
        if not known and not declared:
            return None
        # A model newer than the runtime knows is taken to have OpenAI's whole ladder.
        xhigh = model_supports_xhigh_effort(provider, model) if known else True
        if effort == "max":
            return "xhigh" if xhigh else "high"
        if effort == "xhigh" and not xhigh:
            return "high"
        return effort
    if not effort or not model_supports_effort(provider, model):
        return None
    if provider == "anthropic":
        if effort == "xhigh" and not model_supports_xhigh_effort(provider, model):
            return "high"
        if effort == "max" and not model_supports_max_effort(provider, model):
            return "high"
        return effort
    if provider == "google":
        # The effort a Gemini model is asked for is one of its own thinking levels.
        return resolve_google_thinking_level(model, effort)
    return None
