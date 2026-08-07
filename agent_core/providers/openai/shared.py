from __future__ import annotations

from ..errors import retryDelayFromLadder

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_INSTRUCTIONS = "You are a helpful and reliable assistant."
OPENAI_ABORT_POLL_INTERVAL_MS = 50


def providerRetryDelayMs(attempt: int) -> int:
    # Call-time import: the config package imports providers at module load.
    from ...config._knobs import knobValue

    return retryDelayFromLadder(tuple(knobValue("provider.openai_retry_delays_ms")), attempt)
