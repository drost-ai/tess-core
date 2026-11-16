"""Waiting for a runtime to finish what it started.

A turn returns when the model stops calling tools, but work it began can go
on: a sub-agent keeps running in its own process, and when it reports, the
runtime wakes its parent with another turn. ``runtime.wait_until_idle()``
returns when all of that has played out. It does not wait for background
shell commands, monitors or scheduled jobs, which may be meant to run for as
long as the host does.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..storage.session_archive import load_session
from .session_inspect import inspect_children
from .session_wake import pending_child_work

POLL_SECONDS = 0.25
# A sub-agent's end and the announcement of its result are two writes: idle
# must be seen twice in a row before it is believed.
QUIET_CHECKS = 2


async def outstanding_work(runtime: Any) -> list[str]:
    """Why the runtime is not idle, one line per reason; empty when it is."""
    manager = runtime.sessions
    reasons = [f"session {session_id}: a turn is running or queued" for session_id, live in list(manager._live.items()) if live.busy()]
    reasons += [
        f"session {session_id}: {item.origin} work is {item.state}"
        for session_id, item in list(manager.pending._items.items())
        if item.state != "refused"
    ]
    children = await inspect_children()
    for entries in children.values():
        for child in entries:
            if child.get("state") == "working" or (child.get("state") == "idle" and child.get("queuedMessages")):
                reasons.append(f"sub-agent {child.get('name') or child.get('agentId')}: working")
    for parent in {*children, *manager._live}:
        if manager.is_ended(parent):
            continue
        live = manager._live.get(parent)
        record = live.record if live is not None else await load_session(parent)
        if isinstance(record, dict) and await pending_child_work(record):
            reasons.append(f"session {parent}: a sub-agent's message is waiting to be read")
    return reasons


async def wait_until_idle(runtime: Any, timeout: float | None = None) -> None:
    deadline = None if timeout is None else time.monotonic() + timeout
    quiet = 0
    while not runtime.stopped:
        reasons = await outstanding_work(runtime)
        quiet = 0 if reasons else quiet + 1
        if quiet >= QUIET_CHECKS:
            return
        if deadline is not None and time.monotonic() >= deadline:
            shown = "; ".join(reasons[:5]) or "it had only just gone quiet"
            raise TimeoutError(f"The runtime was not idle after {timeout:g} seconds: {shown}")
        await asyncio.sleep(POLL_SECONDS)
