"""Parent wake controller: turn subagent.message events into continuity turns.

Gated by the runtime's turn-in-flight flag — while a turn runs, the agent
loop's boundary drain batches everything queued, so the controller does
nothing. When idle, it invokes the embedder-provided resume callback once
per burst; the resume runs a continuity turn (run_parent_continuity_turn /
run_agent_prompt with an empty prompt) that drains the mailbox into the
transcript and lets the model react.

The mailbox watch is LEVEL-TRIGGERED: a still-queued message is
re-announced until a drain claims it, so this controller is the deduper —
one resume per idle burst, re-armed when a turn starts or when a pending
resume expires without a turn ever starting (a dropped resume must not
block future wakes; that was the original lost-wake bug in another form).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

_PENDING_RESUME_EXPIRY_SECONDS = 10.0


def create_mailbox_wake_controller(*, runtime: dict[str, Any], resume: Callable[[], Any]) -> dict[str, Any]:
    is_turn_in_flight = runtime.get("isTurnInFlight") or (lambda: False)
    guard = threading.Lock()
    closed = False
    pending_since: float | None = None

    def wake() -> bool:
        nonlocal closed, pending_since
        with guard:
            if closed:
                return False
            if is_turn_in_flight():
                # A turn is running; its boundary drain owns everything
                # queued. Re-arm so the next idle announcement wakes again.
                pending_since = None
                return False
            now = time.monotonic()
            if pending_since is not None and now - pending_since < _PENDING_RESUME_EXPIRY_SECONDS:
                return False
            pending_since = now
        resume()
        return True

    def on_envelope(envelope: dict[str, Any]) -> None:
        if (envelope or {}).get("event") == "subagent.message":
            wake()

    unsubscribe = runtime["subscribeEvents"](on_envelope)

    def close() -> None:
        nonlocal closed
        with guard:
            closed = True
        unsubscribe()

    return {"wake": wake, "close": close}
