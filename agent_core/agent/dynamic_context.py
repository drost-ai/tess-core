from __future__ import annotations

import copy
from typing import Any

from ..storage.message_origin import (
    isGenuineUserRequest as _is_genuine_user_request,
    syntheticUserMessage as _synthetic_user_message,
)
from ..providers.prompt_cache import resolvePromptCachePolicy


def dynamicContextBelongsInMessages(config: dict[str, Any]) -> bool:
    return bool(resolvePromptCachePolicy(config=config).get("enabled"))


def injectSystemContextIntoMessages(
    request_context: dict[str, Any],
    system_context: Any,
    *,
    config: dict[str, Any],
) -> dict[str, Any]:
    if not system_context or not dynamicContextBelongsInMessages(config):
        return request_context
    messages = copy.deepcopy(request_context.get("messages") or [])
    if not messages:
        return {**request_context, "messages": [_dynamic_context_message(system_context)]}

    dynamic_block = _dynamic_context_text_block(system_context)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not _is_genuine_user_request(message):
            continue
        content = message.get("content")
        if isinstance(content, str):
            messages[index] = {
                **message,
                "content": f"{dynamic_block['text']}\n\n{content}",
            }
        elif isinstance(content, list):
            messages[index] = {
                **message,
                "content": [dynamic_block, *copy.deepcopy(content)],
            }
        else:
            messages[index] = {**message, "content": dynamic_block["text"]}
        return {**request_context, "messages": messages}

    messages.append(_dynamic_context_message(system_context))
    return {**request_context, "messages": messages}


def _dynamic_context_message(system_context: Any) -> dict[str, Any]:
    return _synthetic_user_message(
        _dynamic_context_text_block(system_context)["text"],
        kind="dynamic_context",
    )


def _dynamic_context_text_block(system_context: Any) -> dict[str, str]:
    text = str(system_context or "").strip()
    return {
        "type": "text",
        "text": "\n".join(
            [
                "[Dynamic continuation context]",
                text,
                "[/Dynamic continuation context]",
            ]
        ),
    }
