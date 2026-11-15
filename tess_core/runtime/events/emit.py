"""Best-effort runtime event emission.

The runtime's emitEvent hook is caller-supplied; emission must never take
down the work that emits. This is the single swallow-and-continue wrapper —
import it instead of redefining a local _emit/_safe_emit.
"""
from __future__ import annotations

from typing import Any


def safe_emit(runtime: Any, event: str, data: dict[str, Any]) -> None:
    if not isinstance(runtime, dict):
        return
    emitter = runtime.get("emitEvent")
    if not callable(emitter):
        return
    try:
        emitter(event, data)
    except Exception:
        pass
