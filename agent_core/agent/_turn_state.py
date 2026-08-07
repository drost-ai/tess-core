from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..providers.usage_costs import roundUsd as _round_usd


@dataclass
class TurnState:
    """Accumulator state for one logical agent turn.

    Everything the provider-call loop mutates while a turn runs lives here:
    streamed text, merged usage, estimated cost, the provider/tool counters
    the subagent runtime limits meter, and the rollback snapshot that
    cancellation restores. The loop's control flow stays in loop.py; this
    class only owns the data and the projections derived from it.
    """

    active_model: str
    rollback_snapshot: Any
    text_parts: list[str] = field(default_factory=list)
    total_usage: dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0
    tool_count: int = 0
    provider_call_index: int = 0
    started_ms: int = field(default_factory=lambda: int(time.monotonic() * 1000))

    def text(self) -> str:
        return "\n\n".join(self.text_parts).strip()

    def elapsed_ms(self) -> int:
        return int(time.monotonic() * 1000) - self.started_ms

    def limit_state(self) -> dict[str, Any]:
        return {
            "providerCalls": self.provider_call_index,
            "toolCalls": self.tool_count,
            "costUsd": self.cost,
            "elapsedMs": self.elapsed_ms(),
        }

    def result(self, stop_reason: Any) -> dict[str, Any]:
        return {
            "text": self.text(),
            "toolCount": self.tool_count,
            "usage": self.total_usage,
            "model": self.active_model,
            "estimatedCostUsd": _round_usd(self.cost),
            "stopReason": stop_reason,
        }
