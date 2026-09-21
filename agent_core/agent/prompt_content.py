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
