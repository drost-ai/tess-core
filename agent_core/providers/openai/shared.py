from __future__ import annotations

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_INSTRUCTIONS = "You are a helpful and reliable assistant."
OPENAI_REQUEST_START_TIMEOUT_MS = 120_000
OPENAI_STREAM_IDLE_TIMEOUT_MS = 60_000
OPENAI_ABORT_POLL_INTERVAL_MS = 50
_OPENAI_RETRY_DELAYS_MS = (1_000, 3_000, 5_000, 10_000, 30_000)


def providerRetryDelayMs(attempt: int) -> int:
    index = min(max(int(attempt), 1), len(_OPENAI_RETRY_DELAYS_MS)) - 1
    return _OPENAI_RETRY_DELAYS_MS[index]
