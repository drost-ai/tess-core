"""Shared host-owned admission budget; host owns cancellation enforcement."""
from __future__ import annotations

import copy
import json
import math
import threading
import time
from contextvars import ContextVar
from typing import Any


class ExecutionBudgetExceeded(RuntimeError):
    """No further work may be admitted."""


class ExecutionBudget:
    def __init__(self, *, max_provider_calls: int | None = 128, max_tool_calls: int | None = 128,
                 max_seconds: float = 1800, wrapup_fraction: float = 0.8) -> None:
        for name, value in (("max_provider_calls", max_provider_calls), ("max_tool_calls", max_tool_calls)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer or None")
        if not math.isfinite(max_seconds) or max_seconds <= 0:
            raise ValueError("max_seconds must be finite and positive")
        if not math.isfinite(wrapup_fraction) or not 0 < wrapup_fraction < 1:
            raise ValueError("wrapup_fraction must be between zero and one")
        self.max_provider_calls = max_provider_calls
        self.max_tool_calls = max_tool_calls
        self.max_seconds = float(max_seconds)
        self.wrapup_fraction = float(wrapup_fraction)
        self._started = time.monotonic()
        self._deadline = self._started + self.max_seconds
        self._provider_calls = 0
        self._tool_calls = 0
        self._phase = "work"
        self._lock = threading.RLock()

    def tighten_deadline(self, deadline: float) -> None:
        """Apply an absolute time.monotonic() deadline, never extending work.

        Keep the original start, counters and sticky phase. The shortened
        lifetime determines wrapup; a deadline at/before the start exhausts it.
        max_seconds remains the configured upper bound, not a fresh allowance.
        """
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise ValueError("deadline must be a finite monotonic timestamp")
        with self._lock:
            self._deadline = min(self._deadline, deadline)
            self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self._started)
            duration = max(0.0, self._deadline - self._started)
            fraction = max(self._provider_calls / self.max_provider_calls if self.max_provider_calls is not None else 0,
                           self._tool_calls / self.max_tool_calls if self.max_tool_calls is not None else 0,
                           elapsed / duration if duration else 1.0)
            if fraction >= 1:
                self._phase = "exhausted"
            elif fraction >= self.wrapup_fraction and self._phase == "work":
                self._phase = "wrapup"
            return {"phase": self._phase, "provider_calls": self._provider_calls,
                    "tool_calls": self._tool_calls, "elapsed_seconds": elapsed,
                    "remaining_provider_calls": max(0, self.max_provider_calls - self._provider_calls) if self.max_provider_calls is not None else None,
                    "remaining_tool_calls": max(0, self.max_tool_calls - self._tool_calls) if self.max_tool_calls is not None else None,
                    "remaining_seconds": max(0.0, self._deadline - max(now, self._started))}

    def _consume(self, field: str) -> None:
        with self._lock:
            if self.snapshot()["phase"] == "exhausted":
                raise ExecutionBudgetExceeded("Execution budget exhausted; no further work admitted")
            setattr(self, field, getattr(self, field) + 1)
            self.snapshot()

    def consume_provider_call(self) -> None:
        self._consume("_provider_calls")

    def consume_tool_call(self) -> None:
        self._consume("_tool_calls")

    def context(self) -> str:
        state = self.snapshot()
        instruction = ("Wrap up now. Do not start new work; finish or release existing work and report progress."
                       if state["phase"] != "work" else
                       "Work within the remaining budget; preserve time for a final report.")
        return "[Execution budget — authoritative host context]\n" + json.dumps(state, sort_keys=True) + "\n" + instruction


active_execution_budget: ContextVar[ExecutionBudget | None] = ContextVar("tess_core_execution_budget", default=None)


def budget_provider_payload(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """Count each transport attempt and add ephemeral context without persisting it."""
    budget = active_execution_budget.get()
    if budget is None:
        return payload
    budget.consume_provider_call()
    result = copy.deepcopy(payload)
    context = budget.context()
    if kind == "chat":
        result["messages"] = [{"role": "system", "content": context}, *result.get("messages", [])]
    else:
        key = "instructions" if kind == "responses" else "system"
        prior = result.get(key)
        if isinstance(prior, list):
            result[key] = [*prior, {"type": "text", "text": context}]
        else:
            result[key] = (str(prior) + "\n\n" if prior else "") + context
    return result


async def run_with_execution_budget(values, callback):
    token = active_execution_budget.set((values.get("runtime") or {}).get("executionBudget"))
    try:
        return await callback()
    finally:
        active_execution_budget.reset(token)


async def admit_tool(runtime, tool_use, finish_tool_result):
    budget = runtime.get("executionBudget")
    if budget is not None:
        try:
            budget.consume_tool_call()
        except ExecutionBudgetExceeded as error:
            return await finish_tool_result(
                tool_use, {"kind": "synthesized", "content": str(error), "isError": True}, rejected=True,
            )
    return None
