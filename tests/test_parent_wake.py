from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.runtime.wake import createMailboxWakeController
from agent_core.tasks.supervisor_parts.mailbox_watch import create_mailbox_watch_loop


class MailboxWatchSweepTests(AgentCoreTestCase):
    def test_sweep_reannounces_queued_messages_until_delivered(self):
        """Bug regression: the sweep used to announce each message once,
        EVER - if that single announcement landed while the parent's turn was
        in flight, the wake was dropped and the parent idled on a finished
        child's result until the user typed. Level-triggered: a still-queued
        message is re-announced (paced) until a drain claims it."""
        from agent_core.tasks.communication import mark_task_message_delivered
        from agent_core.tasks.supervisor_parts import mailbox_watch as watch_module

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await core.writeTaskMeta("wake-task", {
                    "type": "subagent", "taskId": "wake-task", "id": "wake-task",
                    "sessionId": "wake-child", "session_id": "wake-child",
                    "parent_session_id": "wake-parent", "status": "running",
                    "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                })
                await core.saveTaskRecord({
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
                await core.writeTaskMeta("dying-task", {
                    "type": "subagent", "taskId": "dying-task", "id": "dying-task",
                    "sessionId": "dying-child", "session_id": "dying-child",
                    "parent_session_id": "dying-parent", "status": "failed",
                    "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                })
                await core.saveTaskRecord({
                    "id": "dying-task", "sessionId": "dying-child", "sourceId": "dying-task",
                    "kind": "subagent", "status": "running", "title": "dying worker",
                    "summary": "running", "createdAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z", "owner": "daemon",
                })
                record = await core.sendTaskMessage("dying-task", {
                    "direction": "to_parent", "content": "[turn failed] boom",
                })
                await core.saveTaskRecord({
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
        from agent_core.tasks.communication import append_task_event

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await core.writeTaskMeta("trace-wake", {
                    "type": "subagent", "taskId": "trace-wake", "id": "trace-wake",
                    "sessionId": "trace-child", "session_id": "trace-child",
                    "parent_session_id": "trace-parent", "status": "running",
                    "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                })
                await core.saveTaskRecord({
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
                self.assertEqual(first["trace_event"], "tool.started")
                self.assertEqual(first["tool"], "Read")
                self.assertEqual(first["parent_session_id"], "trace-parent")
                self.assertEqual(events[0][2], "trace-parent")  # routes on the PARENT session

                await append_task_event("trace-wake", {"type": "trace", "event": "tool.completed", "tool": "Read", "output_chars": 120, "error": False})
                self.assertEqual(await sweep(), 1)
            run(exercise())
        finally:
            self.restore_home(root, previous)


class WakeControllerTests(AgentCoreTestCase):
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
        controller = createMailboxWakeController({"runtime": runtime, "resume": lambda: resumed.append(1)})
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
        controller = core.createSessionRuntimeController({
            "session": {"id": "flight-check"}, "config": {}, "emitRuntimeEvent": lambda *_: None,
        })
        self.assertFalse(controller["isTurnInFlight"]())
        controller["startTurn"]({"prompt": "hi"})
        self.assertTrue(controller["isTurnInFlight"]())
        controller["finishTurn"]({"stopReason": "end_turn"})
        self.assertFalse(controller["isTurnInFlight"]())
        controller["startTurn"]({"prompt": "again"})
        controller["failTurn"]("boom")
        self.assertFalse(controller["isTurnInFlight"]())

    def test_continuity_turn_runs_agent_prompt_with_empty_prompt(self):
        import agent_core.agent.continuity as continuity_module

        captured = {}

        async def fake_run(values):
            captured.update(values)
            return {"stopReason": "end_turn", "text": ""}

        with mock.patch.object(continuity_module, "runAgentPrompt", fake_run):
            result = run(core.runParentContinuityTurn({"session": {"id": "s"}, "config": {}, "runtime": {}}))
        self.assertEqual(result["stopReason"], "end_turn")
        self.assertEqual(captured["prompt"], "")
        self.assertEqual(captured["session"], {"id": "s"})


if __name__ == "__main__":
    unittest.main()
