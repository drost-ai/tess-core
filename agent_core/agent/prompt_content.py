from __future__ import annotations

import copy
from typing import Any


def normalize_agent_prompt(value: Any) -> tuple[str | list[dict[str, Any]] | None, str]:
    if isinstance(value, str):
        return (value, value) if value.strip() else (None, "")
    if not isinstance(value, list):
        raise TypeError("Agent prompt must be text or a list of content blocks.")
    blocks = [copy.deepcopy(block) for block in value if isinstance(block, dict)]
    if not blocks:
        return None, ""
    text = "\n".join(
        str(block.get("text") or "").strip()
        for block in blocks
        if str(block.get("type") or "") in {"text", "input_text"} and str(block.get("text") or "").strip()
    )
    return blocks, text


def extract_assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "")
        for block in content or []
        if block.get("type") == "text"
    )


def host_system_prompt(base: str, ephemeral_context: str | None = None) -> str:
    """Compose per-call host context without mutating persisted or inherited prompts."""
    if ephemeral_context is None:
        return base
    if not isinstance(ephemeral_context, str):
        raise ValueError("ephemeralSystemContext must be a string")
    return base + "\n\n" + ephemeral_context if ephemeral_context else base


def host_turn_limits(existing, requested=None):
    """Explicit per-call host budgets only tighten inherited execution limits."""
    if requested is None:
        return existing
    mapping = {"maxProviderCalls": "maxTurns", "maxToolCalls": "maxToolCalls", "timeoutMs": "timeoutMs"}
    if not isinstance(requested, dict) or set(requested) - set(mapping):
        raise ValueError("turnLimits contains unsupported fields")
    result = dict(existing or {})
    for key, value in requested.items():
        if type(value) is not int or value <= 0:
            raise ValueError("turnLimits values must be positive integers")
        target = mapping[key]
        result[target] = min(result[target], value) if target in result else value
    return result or None
