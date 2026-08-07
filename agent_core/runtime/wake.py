"""Parent wake controller: turn subagent.message events into continuity turns.

Gated by the runtime's turn-in-flight flag — while a turn runs, the agent
loop's boundary drain batches everything queued, so the controller does
nothing. When idle, it invokes the embedder-provided resume callback once
per burst; the resume runs a continuity turn (runParentContinuityTurn /
runAgentPrompt with an empty prompt) that drains the mailbox into the
transcript and lets the model react. The end-of-turn final drain plus this
idle check are the only decision points, closing the message-arrives-as-
turn-ends race.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


def createMailboxWakeController(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    runtime = values["runtime"]
    resume: Callable[[], Any] = values["resume"]
    is_turn_in_flight = runtime.get("isTurnInFlight") or (lambda: False)
    guard = threading.Lock()
    closed = False

    def wake() -> bool:
        nonlocal closed
        with guard:
            if closed or is_turn_in_flight():
                return False
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
