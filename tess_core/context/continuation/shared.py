from __future__ import annotations

import json
from typing import Any


LOW_VALUE_TOOL_NAMES = {"SetStatus", "TodoWrite", "Config"}


from .._knob_reader import context_knob as relief_knob


def _tool_result_content_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return str(value)
