"""Shared fixtures for pending child work reopening a closed parent: a
scripted provider, a recording listener, seeded tasks and the knobs the
tests shorten. Used by test_session_wake.py and its reconciliation tests."""
from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core import start_runtime
from agent_core.storage.records import save_task_record
from agent_core.tasks.communication import create_task_question, send_task_message

IDLE = "AGENT_CORE_SESSIONS_IDLE_CLOSE_MS"
WATCH = "AGENT_CORE_DAEMON_MAILBOX_WATCH_MS"
RETRY = "AGENT_CORE_SESSIONS_PENDING_RETRY_MS"
FAIR = "AGENT_CORE_SESSIONS_BACKGROUND_MAX_WAIT_MS"
REPLY = "AGENT_CORE_COMMS_REPLY_TIMEOUT_MS"
_ENV = (IDLE, WATCH, RETRY, FAIR, REPLY, "AGENT_CORE_TEST_MISSING_WAKE_KEY")


class Listener:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.opened: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.runtime_events: list[tuple[str, dict]] = []

    def on_event(self, session_id, envelope):
        self.events.append((session_id, envelope["event"], envelope["data"]))

    def on_session_opened(self, session_id, reason):
        self.opened.append((session_id, reason))

    def on_session_closed(self, session_id, reason):
        self.closed.append((session_id, reason))

    def on_runtime_event(self, event, data):
        self.runtime_events.append((event, data))

    def turn_starts(self, session_id):
        return [data for sid, event, data in self.events if sid == session_id and event == "turn.started"]


class Provider:
    """A fake provider that keeps every request it saw."""

    def __init__(self, text="ok", *, fail=False):
        self.requests: list[dict] = []
        self.text = text
        self.fail = fail

    async def send(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider exploded")
        return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": self.text}]}

    @property
    def override(self):
        return {"send": self.send}

    def transcripts(self):
        return [json.dumps(request["messages"]) for request in self.requests]


async def _start(home, workspace, listener, **kwargs):
    return await start_runtime(str(home), cwd=str(workspace), listener=listener, startup_timeout_seconds=20, **kwargs)


async def _wait(predicate, timeout=20.0, label=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timeout waiting for {label}")


async def _seed_task(workspace, parent_id, task_id, *, status="completed", message=None, question=None):
    """A child's task on disk, as the daemon would leave it: meta, record and
    optionally a queued to_parent message or a pending question."""
    child_id = f"child-{task_id}"
    await core.write_task_meta(task_id, {
        "type": "subagent", "taskId": task_id, "id": task_id, "sessionId": child_id, "session_id": child_id,
        "parent_session_id": parent_id, "status": status, "cwd": str(workspace),
        "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
    })
    await save_task_record({
        "id": task_id, "sessionId": parent_id, "kind": "subagent", "status": status, "title": task_id,
        "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
    })
    if message is not None:
        await send_task_message(task_id, {"direction": "to_parent", "content": message, "kind": "result", "parentSessionId": parent_id})
    if question is not None:
        await create_task_question(task_id, {"childSessionId": child_id, "question": question, "options": ["main", "dev"]})


async def _adopt_on_disk(parent_id, task_id):
    """The parent's record names its child's task (what the spawn does)."""
    record = await core.load_session(parent_id)
    record["metadata"]["taskIds"] = [*(record["metadata"].get("taskIds") or []), task_id]
    await core.save_session(record)
