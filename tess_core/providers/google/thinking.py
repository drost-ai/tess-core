"""Which thinking levels a Gemini model takes, and the one a request asks for.

The Interactions API sets reasoning depth with ``generation_config.thinking_level``.
Each model accepts its own subset of the levels and refuses the rest, so a request
names a level only when the model is known to take it. A model this table does not
know is sent no level and thinks at its own default.

Reviewed against https://aistudio.google.com/docs/thinking on 2026-11-19.
"""
from __future__ import annotations

GOOGLE_THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

_LOW_TO_HIGH = ("low", "medium", "high")
_LOW_TO_XHIGH = ("low", "medium", "high", "xhigh")
_MINIMAL_TO_HIGH = ("minimal", "low", "medium", "high")

# Longest prefix wins, so a more specific entry goes before the family it belongs to.
_LEVELS_BY_MODEL_PREFIX: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gemini-4-neon", _LOW_TO_XHIGH),
    ("gemini-4-hydrogen", _LOW_TO_XHIGH),
    ("gemini-4-carbon", _LOW_TO_HIGH),
    ("gemini-4-argon", _LOW_TO_HIGH),
    ("gemini-4-", _LOW_TO_HIGH),
    ("gemini-3.8-flash", _LOW_TO_HIGH),
    ("gemini-3.6-flash", _MINIMAL_TO_HIGH),
    ("gemini-3.5-flash-lite", _MINIMAL_TO_HIGH),
    ("gemini-3.1-flash-lite-image", ("minimal", "high")),
    ("gemini-3.1-flash-lite", _MINIMAL_TO_HIGH),
    ("gemini-3.1-pro", _LOW_TO_HIGH),
    ("gemini-3-flash", _MINIMAL_TO_HIGH),
    ("gemini-3-pro", ("low", "high")),
    ("gemini-2.5-pro", _LOW_TO_HIGH),
    ("gemini-2.5-flash", _LOW_TO_HIGH),
)


def google_thinking_levels(model: str | None) -> tuple[str, ...]:
    """The thinking levels ``model`` accepts, lowest first; empty when it is not known."""
    name = str(model or "").strip().lower()
    if name.startswith("models/"):
        name = name[len("models/"):]
    best: tuple[str, tuple[str, ...]] | None = None
    for prefix, levels in _LEVELS_BY_MODEL_PREFIX:
        if name.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, levels)
    return best[1] if best else ()


def resolve_google_thinking_level(model: str | None, effort: str | None) -> str | None:
    """The level to send for a requested effort, or None to leave the model's default.

    ``max`` is the model's deepest level. A level the model does not take becomes the
    nearest one below it that it does, or its lowest if there is none below.
    """
    levels = google_thinking_levels(model)
    requested = str(effort or "").strip().lower()
    if not levels or not requested:
        return None
    if requested == "max":
        return levels[-1]
    if requested not in GOOGLE_THINKING_LEVELS:
        return None
    if requested in levels:
        return requested
    rank = GOOGLE_THINKING_LEVELS.index(requested)
    below = [level for level in levels if GOOGLE_THINKING_LEVELS.index(level) < rank]
    return below[-1] if below else levels[0]
