from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.runtime.wake import createMailboxWakeController
from agent_core.tasks.supervisor_parts.mailbox_watch import create_mailbox_watch_loop


class MailboxWatchSweepTests(AgentCoreTestCase):
    def test_sweep_announces_queued_to_parent_messages_exactly_once(self):
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
                self.assertEqual(await sweep(), 0)  # announced once, ever
                event, data, session_id = events[0]
                self.assertEqual(event, "subagent.message")
                self.assertEqual(data["task_id"], "wake-task")
                self.assertEqual(data["message_id"], record["messageId"])
                self.assertEqual(data["parent_session_id"], "wake-parent")
                self.assertEqual(session_id, "wake-parent")
                self.assertIn("first result", data["preview"])

                # to_child traffic is not a parent-wake signal
                await core.sendTaskMessage("wake-task", {"direction": "to_child", "content": "guidance"})
                self.assertEqual(await sweep(), 0)
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

        listeners[0]({"event": "task.updated", "data": {}})
        self.assertEqual(resumed, [1])  # other events do not wake

        controller["close"]()
        self.assertEqual(listeners, [])
        self.assertFalse(controller["wake"]())
        self.assertEqual(resumed, [1])

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
