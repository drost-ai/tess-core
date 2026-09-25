"""status() and inspect() answer the ownership questions from one call, with
identifiers and lifecycle reasons only, against real daemon and child
processes. Every test is one coroutine.
"""
from __future__ import annotations

from tests.support.agent_core_case import *
from tests.support.session_monitors import LEASE, Listener as MonitorListener, ScriptedProvider, complete, registered_runtime, tool_reply
from tests.test_session_end import Listener, _child_pid, _reply, _settings, _spawn_child, _wait, _wait_async

from agent_core import SessionOptions, start_runtime
from agent_core.config._knobs import reset_configured_knobs
from agent_core.runtime import host as host_module
from agent_core.tasks.communication import send_task_message

_ENV = ("AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS", "AGENT_CORE_TEST_MISSING_END_KEY", LEASE)


class RuntimeStatusTests(AgentCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None
        os.environ["AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS"] = "5000"  # a queued message stays queued long enough to be seen

    def tearDown(self):
        for name in _ENV:
            os.environ.pop(name, None)
        reset_configured_knobs()

    def test_inspect_answers_the_ownership_questions_against_real_processes(self):
        root, home, workspace, previous = self.temp_context()
        try:
            settings_path = _settings(home)

            def options_for(session_id, reason):
                return SessionOptions(provider="end-openai", settings=str(settings_path), permission_mode="bypassPermissions", provider_override=_reply())

            async def scenario():
                listener = Listener()
                runtime = await start_runtime(str(home), cwd=str(workspace), listener=listener, session_options=options_for, startup_timeout_seconds=20)
                try:
                    handle = await runtime.sessions.open()
                    await handle.run_turn("start a child")
                    config = runtime.sessions.live(handle.id).config
                    child_session, task_id = await _spawn_child(runtime, handle.id, "child-status", settings_path, workspace, config)
                    await _wait_async(lambda: _child_pid("child-status"), label="child alive")
                    await _wait_async(lambda: _idle_child("child-status"), label="child idle after its first turn")
                    await send_task_message(task_id, {"direction": "to_child", "content": "one more thing"})

                    view = await runtime.inspect()
                    daemon = view["daemon"]
                    self.assertEqual((daemon["ownerPid"], daemon["recoveries"], daemon["connected"]), (os.getpid(), 0, True))
                    self.assertTrue(daemon["pid"] and daemon["instanceId"] == runtime.instance_id)
                    self.assertEqual([s["id"] for s in view["sessions"]["open"]], [handle.id])
                    self.assertEqual(view["sessions"]["open"][0]["state"], "idle")
                    self.assertGreaterEqual(view["sessions"]["open"][0]["idleSeconds"], 0.0)
                    self.assertEqual(view["sessions"]["pending"], [])
                    self.assertEqual(view["sessions"]["recent"]["opened"]["reason"], "client")
                    await _wait(lambda: runtime.status()["sessions"]["recent"]["spawned"], label="the spawn cause")
                    self.assertEqual(runtime.status()["sessions"]["recent"]["spawned"]["taskId"], task_id)
                    children = view["children"][handle.id]
                    self.assertEqual(len(children), 1)
                    child = children[0]
                    self.assertEqual((child["agentId"], child["taskId"], child["sessionId"], child["state"]), ("child-status", task_id, child_session, "idle"))
                    self.assertGreaterEqual(child["idleSeconds"], 0.0)
                    self.assertLessEqual(child["timeLeftSeconds"], 900.0)
                    self.assertEqual((child["queuedMessages"], child["pendingQuestions"]), (1, 0))
                    self.assertTrue(child["pid"])
                    self.assertEqual(view["monitorDeliveries"], [])
                    self.assertTrue(view["observedAt"])
                    self.assertNotIn("messages", json.dumps(view))  # ids and reasons only

                    await runtime.sessions.end(handle.id)
                    view = await runtime.inspect()
                    self.assertEqual(view["sessions"]["open"], [])
                    self.assertEqual(view["children"][handle.id][0]["state"], "stopped")
                    self.assertEqual(view["children"][handle.id][0]["reason"], "session_ended")
                    self.assertEqual(view["sessions"]["recent"]["closed"]["reason"], "ended")
                    await _wait(lambda: runtime.status()["sessions"]["recent"]["stopped"], label="the stop cause")
                    self.assertEqual(runtime.status()["sessions"]["recent"]["stopped"]["taskId"], task_id)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_inspect_lists_monitor_deliveries_with_their_lifecycle_phase(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ.pop(LEASE, None)   # nothing configured: the delivery waits, visibly

            async def scenario():
                provider = ScriptedProvider(tool_reply("TaskMonitorStart", {"task_id": "build", "goal": "watch build"}), "Registered.")
                listener = MonitorListener()
                runtime, handle = await registered_runtime(home, workspace, listener, provider)
                try:
                    await complete(runtime)
                    view = await runtime.inspect()
                    self.assertEqual(view["sessions"]["monitors"]["error"], "monitor.lease_not_configured")
                    deliveries = view["monitorDeliveries"]
                    self.assertEqual(len(deliveries), 1)
                    self.assertEqual((deliveries[0]["sessionId"], deliveries[0]["mode"], deliveries[0]["state"], deliveries[0]["phase"], deliveries[0]["reason"]),
                                     (handle.id, "notification", "pending", "waiting_for_consumer", "completed"))
                    self.assertNotIn("UNIQUE_MONITOR_MARKER", json.dumps(view))  # never task output
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)


async def _idle_child(agent_id):
    record = await core.load_child_agent(agent_id)
    return record if record and record.get("status") == "awaiting_input" else None
