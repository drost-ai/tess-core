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
