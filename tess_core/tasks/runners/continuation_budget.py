"""Strict per-admission continuation ceilings composed with inherited budgets.

Implements the execution-budget context protocol (consume_provider_call,
consume_tool_call, context). Unlike loop-boundary turnLimits, this also fences
multiple tools in one provider batch. Missing limits remain unbounded; the
inherited budget is reused, never reset or replaced with a fresh allowance.
"""
from __future__ import annotations

import json
import threading
import time

from ...runtime.execution_budget import ExecutionBudgetExceeded


class ContinuationBudget:
    def __init__(self, limits, inherited=None):
        self.limits = dict(limits or {})
        self.inherited = inherited
        self.counts = {"maxProviderCalls": 0, "maxToolCalls": 0}
        timeout = self.limits.get("timeoutMs")
        self.deadline = time.monotonic() + timeout / 1000 if timeout is not None else None
        self.lock = threading.RLock()

    def _consume(self, key, method):
        with self.lock:
            cap = self.limits.get(key)
            if (self.deadline is not None and time.monotonic() >= self.deadline
                    or cap is not None and self.counts[key] >= cap):
                raise ExecutionBudgetExceeded("Monitor continuation budget exhausted; no further work admitted")
            # Admit against the inherited budget too. A local rejection must
            # not spend its counters; an inherited rejection cannot be reset.
            if self.inherited is not None:
                getattr(self.inherited, method)()
            self.counts[key] += 1

    def consume_provider_call(self):
        self._consume("maxProviderCalls", "consume_provider_call")

    def consume_tool_call(self):
        self._consume("maxToolCalls", "consume_tool_call")

    def context(self):
        with self.lock:
            remaining = {key: max(0, self.limits[key] - value)
                         for key, value in self.counts.items() if key in self.limits}
            if self.deadline is not None:
                remaining["remainingSeconds"] = max(0, self.deadline - time.monotonic())
            local = "[Monitor continuation limits — authoritative runtime context]\n" + json.dumps(remaining, sort_keys=True)
            return (self.inherited.context() + "\n" if self.inherited is not None else "") + local
