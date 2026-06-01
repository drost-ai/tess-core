from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from ..compaction import buildCompactionSystemPromptContext, buildMessagesForWorkingContext
from ..durable_memory import renderRetrievedMemoryPrompt, retrieveRelevantDurableMemory


RELIEF_PRESERVE_MIN_MESSAGES = 12
RELIEF_PREFERRED_RAW_TAIL_TOKENS = 24_000
RELIEF_MESSAGE_TEXT_CHAR_LIMIT = 1_200
RELIEF_TOOL_INPUT_CHAR_LIMIT = 500
RELIEF_TOOL_RESULT_CHAR_LIMIT = 750
LOW_VALUE_TOOL_NAMES = {"SetStatus", "TodoWrite", "Config"}
RELIEF_CONTEXT_TOKEN_LIMIT = 200_000
