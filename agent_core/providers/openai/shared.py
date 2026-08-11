from __future__ import annotations

from typing import Any

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_INSTRUCTIONS = "You are a helpful and reliable assistant."
OPENAI_ABORT_POLL_INTERVAL_MS = 50


def openai_retry_delay_ms(attempt: int, failure: Any = None) -> int:
    # Call-time import: the config package imports providers at module load.
    from ...config._knobs import knobValue
    from .._core import provider_retry_delay_ms

    return provider_retry_delay_ms(
        attempt,
        ladder=tuple(knobValue("provider.openai_retry_delays_ms")),
        failure=failure,
    )
