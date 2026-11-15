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

from .image_redaction import redact_image_payloads


def estimate_text_tokens(value: str) -> int:
    return (len(value) + 3) // 4


def stringify_message_for_token_estimate(message: dict[str, Any]) -> str:
    content = message.get("content")
    rendered = (
        content
        if isinstance(content, str)
        else json.dumps(redact_image_payloads(content), ensure_ascii=False, separators=(",", ":"))
    )
    return f"{message.get('role')}:{rendered}"


def stringify_value_for_token_estimate(value: Any) -> str:
    return json.dumps(redact_image_payloads(value), ensure_ascii=False, separators=(",", ":"))


def estimate_single_message_tokens(message: dict[str, Any]) -> int:
    return estimate_text_tokens(stringify_message_for_token_estimate(message))


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_single_message_tokens(message) for message in messages)
