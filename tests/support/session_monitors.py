"""Shared fixtures for monitor delivery through the runtime: a scripted
provider, a host listener with an idempotent journal, a registered monitor.
Ported from tess-agent's tests/support.py and test_monitor_delivery.py."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import agent_core._facade as core
from agent_core import SessionOptions, start_runtime
from agent_core._utils import now_iso
from agent_core.storage import list_monitors, save_monitor, task_output_path, write_task_meta

MARKER = "UNIQUE_MONITOR_MARKER"
LEASE = "AGENT_CORE_SESSIONS_MONITOR_CLAIM_LEASE_MS"
DEADLINE = "AGENT_CORE_SESSIONS_MONITOR_TOTAL_DEADLINE_MS"
CLEANUP = "AGENT_CORE_SESSIONS_MONITOR_CLEANUP_MS"
POLL = "AGENT_CORE_SESSIONS_MONITOR_POLL_MS"
IDLE = "AGENT_CORE_SESSIONS_IDLE_CLOSE_MS"
MONITOR_ENV = (LEASE, DEADLINE, CLEANUP, POLL, IDLE)


def text_reply(text: str) -> dict[str, Any]:
    return {"model": "scripted", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": text}]}


def tool_reply(name: str, tool_input: dict[str, Any], *, text: str = "", tool_use_id: str = "tu_1") -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    content.append({"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input})
    return {"model": "scripted", "stopReason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": content}


class ScriptedProvider:
    """Replays canned responses and records requests; ``gate`` holds every
    request until the test sets it."""

    def __init__(self, *replies: str | dict[str, Any], gated: bool = False) -> None:
        self.replies: list[str | dict[str, Any]] = list(replies) or ["ok"]
        self.requests: list[dict[str, Any]] = []
        self.answered = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.gate = asyncio.Event()
        if not gated:
            self.gate.set()

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(json.loads(json.dumps(request, default=str)))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await self.gate.wait()
        finally:
            self.in_flight -= 1
        reply = self.replies[min(self.answered, len(self.replies) - 1)]
        self.answered += 1
        return reply if isinstance(reply, dict) else text_reply(reply)

    @property
    def override(self) -> dict[str, Any]:
        return {"send": self.send}

    def transcript(self, index: int) -> str:
        return json.dumps(self.requests[index].get("messages"))


class Listener:
    """The host: events, questions (denied by default) and a durable,
    idempotent journal keyed by delivery id."""

    def __init__(self, *, answer: str = "") -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.opened: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.runtime_events: list[tuple[str, dict]] = []
        self.journal: dict[str, dict[str, Any]] = {}
        self.published: list[dict[str, Any]] = []
        self.fail_publish: Exception | None = None
        self.answer = answer
        self.seq = 0

    def on_event(self, session_id, envelope):
        self.events.append((session_id, envelope["event"], envelope["data"]))

    def on_session_opened(self, session_id, reason):
        self.opened.append((session_id, reason))

    def on_session_closed(self, session_id, reason):
        self.closed.append((session_id, reason))

    def on_runtime_event(self, event, data):
        self.runtime_events.append((event, data))

    async def ask_question(self, session_id, question, options, detail):
        return self.answer

    async def publish(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_publish is not None:
            raise self.fail_publish
        receipt = self.journal.get(payload["deliveryId"])
        if receipt is None:
            self.seq += 1
            receipt = {"deliveryId": payload["deliveryId"], "sessionId": session_id, "seq": self.seq, "publishedAt": now_iso(), "visible": True}
            self.journal[payload["deliveryId"]] = receipt
            self.published.append({**payload, "receipt": receipt})
        return receipt

    def notices(self, session_id: str) -> list[dict[str, Any]]:
        return [payload for payload in self.published if payload["sessionId"] == session_id]

    def turn_starts(self, session_id: str) -> list[dict]:
        return [data for sid, event, data in self.events if sid == session_id and event == "turn.started"]


class SilentListener(Listener):
    """A host that cannot publish at all."""

    publish = None  # type: ignore[assignment]


async def eventually(condition, *, timeout: float = 5.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition was not met in time: {label}")
        await asyncio.sleep(0.01)


async def settled(runtime, *, timeout: float = 5.0) -> None:
    """No pending work and no running turn in any session."""

    def quiet():
        # Quarantined cleanup may still be pending: that is the point of some tests.
        sessions = runtime.status()["sessions"]
        return not sessions["pending"] and all(entry["turn"] is None for entry in sessions["open"])

    await eventually(quiet, timeout=timeout, label="runtime settled")


async def seed_task(runtime, session_id: str, workspace) -> None:
    """A benign background task the session owns, with distinctive output."""
    await write_task_meta("build", {"taskId": "build", "id": "build", "sessionId": session_id, "status": "running",
                                    "type": "background_command", "pid": os.getpid(), "command": "benign fixture", "cwd": str(workspace)})
    Path(task_output_path("build")).write_text(MARKER + "\n")
    live = runtime.sessions.live(session_id)
    live.record.setdefault("metadata", {})["taskIds"] = ["build"]
    await core.persist_session(live.record, live.config)


async def registered_runtime(home, workspace, listener, provider, *, mode: str = "notification", daemon_enabled: bool = False,
                             permission_mode: str = "bypassPermissions", monitor_continuation: bool = True, options_for=None):
    """A runtime with one session that registered a monitor on the seeded
    task through the assistant (the scripted provider's first reply is the
    TaskMonitorStart call)."""

    def options(session_id, reason):
        return SessionOptions(permission_mode=permission_mode, provider_override=provider.override)

    runtime = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=daemon_enabled,
                                  session_options=options_for or options, startup_timeout_seconds=20, monitor_continuation=monitor_continuation)
    handle = await runtime.sessions.open()
    await seed_task(runtime, handle.id, workspace)
    await handle.run_turn("Monitor the build" + (" and inspect the result with an allowed read" if mode == "continuation" else ""))
    return runtime, handle


async def complete(runtime, *, poll: bool = True) -> dict[str, Any]:
    """The watched task finishes; the next observation sees it."""
    monitor = (await list_monitors())[0]
    await write_task_meta("build", {"taskId": "build", "sessionId": monitor["sessionId"], "status": "completed", "exitCode": 0})
    monitor["lastCheckAt"] = "2000-01-01T00:00:00Z"
    await save_monitor(monitor)
    if poll:
        await runtime.sessions.monitors.poll()
    return monitor


async def make_available(monitor_id: str) -> None:
    record = await core.load_monitor(monitor_id)
    record["delivery"]["queue"][0]["availableAt"] = 0
    await save_monitor(record)
