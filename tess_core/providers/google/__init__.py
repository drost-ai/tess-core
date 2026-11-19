"""Google adapter: the Gemini Interactions API, stateless, over the runtime's own transport."""
from __future__ import annotations

from .params import (
    DEFAULT_GOOGLE_BASE_URL,
    GOOGLE_PROVIDER_LABEL,
    build_interactions_create_params,
    convert_messages_to_steps,
    resolve_google_base_url,
    sanitize_google_schema,
)
from .request import send_google_interactions_request
from .thinking import GOOGLE_THINKING_LEVELS, google_thinking_levels, resolve_google_thinking_level

__all__ = [
    "DEFAULT_GOOGLE_BASE_URL",
    "GOOGLE_PROVIDER_LABEL",
    "GOOGLE_THINKING_LEVELS",
    "build_interactions_create_params",
    "convert_messages_to_steps",
    "google_thinking_levels",
    "resolve_google_base_url",
    "resolve_google_thinking_level",
    "sanitize_google_schema",
    "send_google_interactions_request",
]
