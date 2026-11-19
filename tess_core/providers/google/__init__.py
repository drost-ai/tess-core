"""Google adapter: the Gemini Interactions API, stateless, over the runtime's own transport."""
from __future__ import annotations

from .thinking import GOOGLE_THINKING_LEVELS, google_thinking_levels, resolve_google_thinking_level

__all__ = [
    "GOOGLE_THINKING_LEVELS",
    "google_thinking_levels",
    "resolve_google_thinking_level",
]
