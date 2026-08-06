from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import re
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..prompt_cache import (
    anthropicAutomaticCacheEnabled,
    anthropicCacheControl,
    anthropicExplicitCacheEnabled,
    isKimiAnthropicCodingBackend,
    resolvePromptCachePolicy,
)
from ..anthropic_cache import (
    applyAnthropicCachePlan,
    countAnthropicCacheControls,
    planAnthropicCache,
    sanitizeAnthropicCountPayload,
)
from ..errors import (
    canRetryProviderFailure,
    classifyProviderHttpFailure,
    createProviderFailureRecord,
    createProviderIncompleteResponseError,
    createProviderRequestError,
    normalizeUnknownProviderFailure,
    providerRetryDelayMs,
)


MESSAGES_API_URL = "https://api.anthropic.com/v1/messages?beta=true"
MESSAGES_COUNT_TOKENS_API_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_REQUEST_START_TIMEOUT_MS = 120_000
ANTHROPIC_STREAM_IDLE_TIMEOUT_MS = 60_000
ANTHROPIC_JSON_RESPONSE_TIMEOUT_MS = 120_000
ANTHROPIC_ABORT_POLL_INTERVAL_MS = 50
