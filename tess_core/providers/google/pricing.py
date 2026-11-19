"""What Gemini models cost, for the usage a session reports.

Reviewed against https://aistudio.google.com/docs/pricing (paid tier, standard
inference, USD per million tokens) on 2026-11-19. Some prices are scheduled to
change; each model lists its prices with the date they take effect, and the one
in force on the day of the call is used.

Thinking tokens are billed as output. Cached input is billed at the caching price,
about a twentieth of the input price. Gemini 3.1 Pro charges more for a prompt over
200,000 tokens; the lower tier is used here, so its long prompts are under-estimated.
"""
from __future__ import annotations

from datetime import date

# model prefix -> ((effective from, input, output, cached input), ...), oldest first.
_PRICES: tuple[tuple[str, tuple[tuple[str, float, float, float], ...]], ...] = (
    ("gemini-4-carbon", (("2026-01-01", 2.0, 10.0, 0.10), ("2026-12-01", 4.0, 20.0, 0.20))),
    ("gemini-4-argon", (("2026-01-01", 2.0, 10.0, 0.10), ("2026-12-01", 4.0, 20.0, 0.20))),
    ("gemini-4-neon", (("2026-01-01", 3.0, 15.0, 0.15),)),
    ("gemini-4-hydrogen", (("2026-01-01", 1.5, 7.5, 0.075),)),
    ("gemini-3.8-flash", (("2026-01-01", 0.75, 3.75, 0.075), ("2027-01-01", 1.5, 7.5, 0.15))),
    ("gemini-3.6-flash", (("2026-01-01", 0.75, 3.75, 0.075), ("2027-01-01", 1.5, 7.5, 0.15))),
    ("gemini-3.5-flash-lite", (("2026-01-01", 0.30, 2.50, 0.03),)),
    ("gemini-3.1-flash-lite", (("2026-01-01", 0.25, 1.50, 0.025),)),
    ("gemini-3.1-pro", (("2026-01-01", 2.0, 12.0, 0.20),)),
)


def resolve_google_model_pricing(model: str | None, today: date | None = None) -> dict[str, float] | None:
    """The price record for a Gemini model on ``today``, or None when it is not listed."""
    name = str(model or "").strip().lower()
    if name.startswith("models/"):
        name = name[len("models/"):]
    best: tuple[str, tuple[tuple[str, float, float, float], ...]] | None = None
    for prefix, prices in _PRICES:
        if name.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, prices)
    if best is None:
        return None
    day = (today or date.today()).isoformat()
    current = best[1][0]
    for price in best[1]:
        if price[0] <= day:
            current = price
    return {
        "inputUsdPerMtok": current[1],
        "outputUsdPerMtok": current[2],
        "cacheWrite5mUsdPerMtok": 0,
        "cacheWrite1hUsdPerMtok": 0,
        "cacheReadUsdPerMtok": current[3],
    }
