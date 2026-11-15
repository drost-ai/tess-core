from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core.runtime.wake import create_mailbox_wake_controller
from tess_core.tasks.supervisor_parts.mailbox_watch import create_mailbox_watch_loop


class MailboxWatchSweepTests(TessCoreTestCase):
    def test_sweep_reannounces_queued_messages_until_delivered(self):
        """Bug regression: the sweep used to announce each message once,
        EVER - if that single announcement landed while the parent's turn was
        in flight, the wake was dropped and the parent idled on a finished
        child's result until the user typed. Level-triggered: a still-queued
        message is re-announced (paced) until a drain claims it."""
        from tess_core.tasks.communication import mark_task_message_delivered
        from tess_core.tasks.supervisor_parts import mailbox_watch as watch_module

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await core.write_task_meta("wake-task", {
                    "type": "subagent", "taskId": "wake-task", "id": "wake-task",
                    "sessionId": "wake-child", "session_id": "wake-child",
                    "parent_session_id": "wake-parent", "status": "running",
                    "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                })
                await core.save_task_record({
                    "id": "wake-task", "sessionId": "wake-child", "sourceId": "wake-task",
                    "kind": "subagent", "status": "running", "title": "wake worker",
                    "summary": "running", "createdAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z", "owner": "daemon",
                })
                events = []

                async def emit(event, data, session_id=None):
                    events.append((event, data, session_id))

                sweep = create_mailbox_watch_loop(emit).sweep_once
                self.assertEqual(await sweep(), 0)

                record = await core.sendTaskMessage("wake-task", {
                    "direction": "to_parent", "content": "first result ready",
                })
                self.assertEqual(await sweep(), 1)
                event, data, session_id = events[0]
                self.assertEqual(event, "subagent.message")
                self.assertEqual(data["task_id"], "wake-task")
                self.assertEqual(data["message_id"], record["messageId"])
                self.assertEqual(data["parent_session_id"], "wake-parent")
                self.assertEqual(session_id, "wake-parent")
                self.assertIn("first result", data["preview"])

                # Re-announcement is paced, not dead: quiet inside the
                # interval, announced again once it elapses.
                for _ in range(watch_module._REANNOUNCE_SWEEPS - 1):
                    self.assertEqual(await sweep(), 0)
                self.assertEqual(await sweep(), 1)

                # Delivery silences it.
                await mark_task_message_delivered("wake-task", record["messageId"])
                for _ in range(watch_module._REANNOUNCE_SWEEPS + 1):
                    self.assertEqual(await sweep(), 0)

                # to_child traffic is not a parent-wake signal
                await core.sendTaskMessage("wake-task", {"direction": "to_child", "content": "guidance"})
                self.assertEqual(await sweep(), 0)
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_sweep_announces_dying_message_of_terminal_task(self):
        """Bug regression: terminal statuses were excluded from the sweep, so
        a child's dying message ([turn failed]) written moments before its
        record went terminal was unannounceable forever."""
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await core.write_task_meta("dying-task", {
                    "type": "subagent", "taskId": "dying-task", "id": "dying-task",
                    "sessionId": "dying-child", "session_id": "dying-child",
                    "parent_session_id": "dying-parent", "status": "failed",
                    "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                })
                await core.save_task_record({
                    "id": "dying-task", "sessionId": "dying-child", "sourceId": "dying-task",
                    "kind": "subagent", "status": "running", "title": "dying worker",
                    "summary": "running", "createdAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z", "owner": "daemon",
                })
                record = await core.sendTaskMessage("dying-task", {
                    "direction": "to_parent", "content": "[turn failed] boom",
                })
                await core.save_task_record({
                    "id": "dying-task", "sessionId": "dying-child", "sourceId": "dying-task",
                    "kind": "subagent", "status": "failed", "title": "dying worker",
                    "summary": "failed", "createdAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:01Z", "owner": "daemon",
                })
                events = []

                async def emit(event, data, session_id=None):
                    events.append((event, data, session_id))

                sweep = create_mailbox_watch_loop(emit).sweep_once
                self.assertEqual(await sweep(), 1)
                announced = [entry for entry in events if entry[0] == "subagent.message"]
                self.assertEqual(announced[0][1]["message_id"], record["messageId"])
                self.assertIn("[turn failed]", announced[0][1]["preview"])
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_sweep_forwards_new_trace_events_exactly_once(self):
        from tess_core.tasks.communication import append_task_event

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await core.write_task_meta("trace-wake", {
                    "type": "subagent", "taskId": "trace-wake", "id": "trace-wake",
                    "sessionId": "trace-child", "session_id": "trace-child",
                    "parent_session_id": "trace-parent", "status": "running",
                    "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                })
                await core.save_task_record({
                    "id": "trace-wake", "sessionId": "trace-child", "sourceId": "trace-wake",
                    "kind": "subagent", "status": "running", "title": "trace worker",
                    "summary": "running", "createdAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z", "owner": "daemon",
                })
                events = []

                async def emit(event, data, session_id=None):
                    events.append((event, data, session_id))

                sweep = create_mailbox_watch_loop(emit).sweep_once
                await append_task_event("trace-wake", {"type": "status", "detail": "not a trace"})
                self.assertEqual(await sweep(), 0)

                await append_task_event("trace-wake", {"type": "trace", "event": "tool.started", "tool": "Read", "input_preview": "file.py"})
                await append_task_event("trace-wake", {"type": "trace", "event": "message.assistant", "text_preview": "reading now"})
                self.assertEqual(await sweep(), 2)
                self.assertEqual(await sweep(), 0)  # cursor: forwarded once, ever

                names = [event for event, _data, _sid in events]
                self.assertEqual(names, ["subagent.trace", "subagent.trace"])
                first = events[0][1]
                self.assertEqual(first["task_id"], "trace-wake")
                # The index is the row's position in the task's event log
                # (row 0 was the non-trace status event).
                self.assertEqual([data["event_index"] for _event, data, _sid in events], [1, 2])
                self.assertEqual(first["trace_event"], "tool.started")
                self.assertEqual(first["tool"], "Read")
                self.assertEqual(first["parent_session_id"], "trace-parent")
                self.assertEqual(events[0][2], "trace-parent")  # routes on the PARENT session

                await append_task_event("trace-wake", {"type": "trace", "event": "tool.completed", "tool": "Read", "output_chars": 120, "error": False})
                self.assertEqual(await sweep(), 1)
                self.assertEqual(events[-1][1]["event_index"], 3)
            run(exercise())
        finally:
            self.restore_home(root, previous)

    async def _subagent_task(self, workspace, task_id, status, *, created_at="2026-01-01T00:00:00Z"):
        await core.write_task_meta(task_id, {
            "type": "subagent", "taskId": task_id, "id": task_id,
            "sessionId": f"{task_id}-child", "session_id": f"{task_id}-child",
            "parent_session_id": f"{task_id}-parent", "status": status,
            "cwd": str(workspace), "startedAt": created_at, "updatedAt": created_at,
        })
        await self._set_status(task_id, status, created_at=created_at)

    async def _set_status(self, task_id, status, *, created_at="2026-01-01T00:00:00Z"):
        await core.save_task_record({
            "id": task_id, "sessionId": f"{task_id}-child", "sourceId": task_id,
            "kind": "subagent", "status": status, "title": task_id,
            "summary": status, "createdAt": created_at, "updatedAt": created_at, "owner": "daemon",
        })

    def test_new_watch_does_not_replay_the_trace_of_a_finished_child(self):
        """Bug regression: the trace cursor lives in memory, so every new watch
        (one per reopened parent runtime) re-forwarded the whole trace history
        of every child, finished ones included. A host that journals what it
        receives showed a child with 59 real steps as 1,062."""
        from tess_core.tasks.communication import append_task_event

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await self._subagent_task(workspace, "old-child", "running")
                for name in ("tool.started", "tool.completed", "message.assistant"):
                    await append_task_event("old-child", {"type": "trace", "event": name})
                await core.sendTaskMessage("old-child", {"direction": "to_parent", "content": "result, still queued"})
                await self._set_status("old-child", "stopped")

                for _restart in range(3):
                    events = []

                    async def emit(event, data, session_id=None, events=events):
                        events.append((event, data))

                    sweep = create_mailbox_watch_loop(emit).sweep_once
                    await sweep()
                    await sweep()
                    names = [event for event, _data in events]
                    self.assertNotIn("subagent.trace", names)
                    # Its queued message is still announced: that is a wake signal, not history.
                    self.assertEqual(names, ["subagent.message"])
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_child_seen_alive_has_its_dying_trace_forwarded(self):
        from tess_core.tasks.communication import append_task_event

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await self._subagent_task(workspace, "dying-trace", "running")
                events = []

                async def emit(event, data, session_id=None):
                    events.append(data)

                sweep = create_mailbox_watch_loop(emit).sweep_once
                await append_task_event("dying-trace", {"type": "trace", "event": "tool.started"})
                self.assertEqual(await sweep(), 1)
                await append_task_event("dying-trace", {"type": "trace", "event": "tool.completed", "error": True})
                await self._set_status("dying-trace", "failed")
                self.assertEqual(await sweep(), 1)
                self.assertEqual([data["event_index"] for data in events], [0, 1])
                self.assertEqual(await sweep(), 0)
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_a_childs_stop_is_announced_once_with_its_reason(self):
        """A host learns that a child stopped (here: its idle time-to-live ran
        out) from the watch, not by re-reading the runtime home."""
        from tess_core.tasks.communication import append_task_event

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await self._subagent_task(workspace, "idle-child", "running")
                events = []

                async def emit(event, data, session_id=None):
                    events.append((event, data, session_id))

                sweep = create_mailbox_watch_loop(emit).sweep_once
                await append_task_event("idle-child", {"type": "trace", "event": "message.assistant"})
                self.assertEqual(await sweep(), 1)
                await append_task_event("idle-child", {
                    "type": "task.subagent.idle_expired", "agentId": "agent-1",
                    "childSessionId": "idle-child-child", "idleTtlMs": 900000,
                })
                await self._set_status("idle-child", "stopped")
                self.assertEqual(await sweep(), 1)
                self.assertEqual(await sweep(), 0)  # announced once, ever

                event, data, session_id = events[-1]
                self.assertEqual(event, "subagent.stopped")
                self.assertEqual(session_id, "idle-child-parent")  # routes on the PARENT session
                self.assertEqual(data, {
                    "task_id": "idle-child", "event_index": 1, "reason": "idle_expired",
                    "agent_id": "agent-1", "child_session_id": "idle-child-child",
                    "parent_session_id": "idle-child-parent",
                })
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_child_that_lived_and_died_between_two_sweeps_is_still_forwarded(self):
        from datetime import datetime, timezone

        from tess_core.tasks.communication import append_task_event

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                events = []

                async def emit(event, data, session_id=None):
                    events.append(data)

                sweep = create_mailbox_watch_loop(emit).sweep_once
                self.assertEqual(await sweep(), 0)
                # Created after the watch started, finished before the next sweep.
                created = datetime.now(timezone.utc).isoformat()
                await self._subagent_task(workspace, "brief-child", "failed", created_at=created)
                await append_task_event("brief-child", {"type": "trace", "event": "tool.started"})
                self.assertEqual(await sweep(), 1)
                self.assertEqual(events[0]["event_index"], 0)
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_live_child_replayed_by_a_new_watch_keeps_the_same_indices(self):
        """A child that is still alive when a new watch starts is forwarded
        again from the beginning; the indices let a consumer drop the repeats."""
        from tess_core.tasks.communication import append_task_event

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await self._subagent_task(workspace, "live-child", "awaiting_input")
                await append_task_event("live-child", {"type": "status", "detail": "not a trace"})
                await append_task_event("live-child", {"type": "trace", "event": "tool.started"})
                await append_task_event("live-child", {"type": "trace", "event": "tool.completed"})
                seen = []
                for _restart in range(2):
                    events = []

                    async def emit(event, data, session_id=None, events=events):
                        events.append((data["event_index"], data["trace_event"]))

                    await create_mailbox_watch_loop(emit).sweep_once()
                    seen.append(events)
                self.assertEqual(seen[0], [(1, "tool.started"), (2, "tool.completed")])
                self.assertEqual(seen[1], seen[0])
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_repeated_announcements_are_marked(self):
        from tess_core.tasks.supervisor_parts import mailbox_watch as watch_module

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await self._subagent_task(workspace, "marked", "running")
                record = await core.sendTaskMessage("marked", {"direction": "to_parent", "content": "done"})
                events = []

                async def emit(event, data, session_id=None):
                    events.append((data["message_id"], data["announcement"]))

                sweep = create_mailbox_watch_loop(emit).sweep_once
                for _ in range(watch_module._REANNOUNCE_SWEEPS + 1):
                    await sweep()
                self.assertEqual(events, [(record["messageId"], "first"), (record["messageId"], "repeat")])
            run(exercise())
        finally:
            self.restore_home(root, previous)


class WakeControllerTests(TessCoreTestCase):
    def _runtime(self, in_flight):
        listeners = []

        def subscribe_events(listener):
            listeners.append(listener)
            return lambda: listeners.remove(listener)

        runtime = {
            "subscribeEvents": subscribe_events,
            "isTurnInFlight": lambda: in_flight["value"],
        }
        return runtime, listeners

    def test_wake_resumes_only_when_idle(self):
        in_flight = {"value": True}
        runtime, listeners = self._runtime(in_flight)
        resumed = []
        controller = create_mailbox_wake_controller(runtime=runtime, resume=lambda: resumed.append(1))
        envelope = {"event": "subagent.message", "data": {"task_id": "t"}}

        listeners[0](envelope)
        self.assertEqual(resumed, [])  # busy: boundary drain will batch it

        in_flight["value"] = False
        listeners[0](envelope)
        self.assertEqual(resumed, [1])

        # The sweep re-announces while queued; the controller dedupes -
        # one resume per idle burst, not one per announcement.
        listeners[0](envelope)
        listeners[0](envelope)
        self.assertEqual(resumed, [1])

        # A turn starting re-arms the controller for the next idle burst.
        in_flight["value"] = True
        listeners[0](envelope)
        self.assertEqual(resumed, [1])
        in_flight["value"] = False
        listeners[0](envelope)
        self.assertEqual(resumed, [1, 1])

        listeners[0]({"event": "task.updated", "data": {}})
        self.assertEqual(resumed, [1, 1])  # other events do not wake

        controller["close"]()
        self.assertEqual(listeners, [])
        self.assertFalse(controller["wake"]())
        self.assertEqual(resumed, [1, 1])

    def test_session_controller_tracks_turn_in_flight(self):
        controller = core.create_session_runtime_controller(
            session={"id": "flight-check"}, config={}, emit=lambda *_: None,
        )
        self.assertFalse(controller["isTurnInFlight"]())
        controller["startTurn"]({"prompt": "hi"})
        self.assertTrue(controller["isTurnInFlight"]())
        controller["finishTurn"]({"stopReason": "end_turn"})
        self.assertFalse(controller["isTurnInFlight"]())
        controller["startTurn"]({"prompt": "again"})
        controller["failTurn"]("boom")
        self.assertFalse(controller["isTurnInFlight"]())

    def test_continuity_turn_runs_agent_prompt_with_empty_prompt(self):
        import tess_core.agent.continuity as continuity_module

        captured = {}

        async def fake_run(values):
            captured.update(values)
            return {"stopReason": "end_turn", "text": ""}

        with mock.patch.object(continuity_module, "run_agent_prompt", fake_run):
            result = run(core.run_parent_continuity_turn({"session": {"id": "s"}, "config": {}, "runtime": {}}))
        self.assertEqual(result["stopReason"], "end_turn")
        self.assertEqual(captured["prompt"], "")
        self.assertEqual(captured["session"], {"id": "s"})


if __name__ == "__main__":
    unittest.main()
