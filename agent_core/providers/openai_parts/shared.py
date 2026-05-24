from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from urllib.parse import urlparse

from ..prompt_cache import normalizeCacheUsageFields, openAiPromptCacheRequestOptions
from ..._utils import compact_json, get_value, maybe_await, to_async_iter
from ...provider_errors import (
    canRetryProviderFailure,
    createProviderFailureRecord,
    createProviderRequestError,
    normalizeUnknownProviderFailure,
    providerRetryDelayMs,
)


DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_INSTRUCTIONS = "You are a helpful and reliable assistant."
OPENAI_REQUEST_START_TIMEOUT_MS = 120_000
OPENAI_STREAM_IDLE_TIMEOUT_MS = 60_000
OPENAI_ABORT_POLL_INTERVAL_MS = 50
