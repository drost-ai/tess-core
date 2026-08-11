from __future__ import annotations

"""The one token-estimation encoding (TS core/message-payloads parity).

Every estimator serializes a message as ``role:content`` — raw string
content, or compact JSON of the image-redacted content — and divides by
four. A second serialization is how the compaction relief gate ended up
subtracting compaction-unit numbers from accounting-unit numbers, 43.5%
apart on metadata-bearing tool results.
"""

import json
from typing import Any

from .image_redaction import redactImagePayloads


def estimateTextTokens(value: str) -> int:
    return (len(value) + 3) // 4


def stringifyMessageForTokenEstimate(message: dict[str, Any]) -> str:
    content = message.get("content")
    rendered = (
        content
        if isinstance(content, str)
        else json.dumps(redactImagePayloads(content), ensure_ascii=False, separators=(",", ":"))
    )
    return f"{message.get('role')}:{rendered}"


def stringifyValueForTokenEstimate(value: Any) -> str:
    return json.dumps(redactImagePayloads(value), ensure_ascii=False, separators=(",", ":"))


def estimateSingleMessageTokens(message: dict[str, Any]) -> int:
    return estimateTextTokens(stringifyMessageForTokenEstimate(message))


def estimateMessageTokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimateSingleMessageTokens(message) for message in messages)
