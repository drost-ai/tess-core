"""Monitor deliveries through the manager: the host's delivery matrix
(tess-agent tests/test_monitor_delivery.py) ported against a fake listener.
The engine's own outbox tests (tests/test_monitor_delivery.py) are untouched.

Every test is one coroutine: a runtime belongs to the loop that started it.
"""
from __future__ import annotations

from unittest.mock import patch

from tests.support.agent_core_case import *
from tests.support.session_monitors import (
    CLEANUP, DEADLINE, IDLE, LEASE, MARKER, MONITOR_ENV, POLL, Listener, ScriptedProvider, SilentListener,
    complete, eventually, make_available, registered_runtime, settled, tool_reply,
)

from agent_core import MonitorDeliveryController, SessionOptions, Unavailable, start_runtime
from agent_core.config._knobs import reset_configured_knobs
from agent_core.runtime import host as host_module
from agent_core.storage import list_monitors, load_monitor


def _register(mode="notification", *more):
    goal = "inspect build" if mode == "continuation" else "watch build"
    fields = {"task_id": "build", "goal": goal}
    if mode == "continuation":
        fields["delivery_mode"] = "continuation"
    return ScriptedProvider(tool_reply("TaskMonitorStart", fields), "Registered.", *more)


class SessionMonitorTests(AgentCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None
        os.environ[LEASE] = "120000"

    def tearDown(self):
        for name in MONITOR_ENV:
            os.environ.pop(name, None)
        reset_configured_knobs()

    def test_registration_then_unsolicited_terminal_is_durable_and_replayed(self):
        for daemon_enabled in (False, True):
            for idle_close in (False, True):
                with self.subTest(daemon=daemon_enabled, idle_close=idle_close):
                    host_module._ACTIVE = None
                    reset_configured_knobs()
                    root, home, workspace, previous = self.temp_context()
                    try:
                        if idle_close:
                            os.environ[IDLE] = "200"
                        else:
                            os.environ.pop(IDLE, None)

                        async def scenario():
                            provider = _register()
                            listener = Listener()
                            runtime, handle = await registered_runtime(home, workspace, listener, provider, daemon_enabled=daemon_enabled)
                            try:
                                if idle_close:
                                    await eventually(lambda: not handle.is_open, label="idle close")
                                monitor = await complete(runtime)
                                await eventually(lambda: listener.notices(handle.id), label="the terminal notice")
                                await settled(runtime)
                                terminal = [n for n in listener.notices(handle.id) if n["reason"] == "completed"]
                                self.assertEqual(len(terminal), 1)
                                self.assertIn("completed", terminal[0]["text"])
                                self.assertEqual(terminal[0]["mode"], "notification")
                                self.assertEqual(provider.answered, 2)  # notification-only: no model call
                                if idle_close:
                                    self.assertIn((handle.id, "monitor"), listener.opened)
                                record = await load_monitor(monitor["id"])
                                self.assertEqual(record["delivery"]["lastReceipt"]["seq"], terminal[0]["receipt"]["seq"])
                                # The notice is in the transcript once, and a second observation republishes nothing.
                                live_or_stored = await core.load_session(handle.id)
                                self.assertEqual(sum(1 for m in live_or_stored["messages"] if (m.get("metadata") or {}).get("monitorDeliveryId")), 1)
                                await runtime.sessions.monitors.poll()
                                await settled(runtime)
                                self.assertEqual(len(listener.notices(handle.id)), 1)
                                self.assertEqual([s["origin"] for s in listener.turn_starts(handle.id)], ["user"])  # no monitor turn for a notification
                            finally:
                                await runtime.stop()

                        run(scenario())
                    finally:
                        self.restore_home(root, previous)

    def test_explicit_continuation_has_evidence_read_access_and_no_new_write_authority(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                provider = _register(
                    "continuation",
                    tool_reply("Read", {"file_path": "evidence.txt"}, tool_use_id="read-evidence"),
                    tool_reply("Write", {"file_path": "unrelated.txt", "content": "forbidden"}, tool_use_id="write-unrelated"),
                    "I inspected the evidence; the build passed.",
                )
                listener = Listener(answer="")  # every permission prompt is denied
                runtime, handle = await registered_runtime(home, workspace, listener, provider, mode="continuation", permission_mode="default")
                Path(workspace, "evidence.txt").write_text("VERIFIED_FILE_EVIDENCE")
                try:
                    await complete(runtime)
                    await eventually(lambda: listener.notices(handle.id), label="the continuation notice")
                    await settled(runtime)
                    terminal = [n for n in listener.notices(handle.id) if n["reason"] == "completed"]
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0]["mode"], "continuation")
                    self.assertEqual(terminal[0]["text"], "I inspected the evidence; the build passed.")
                    self.assertIn(MARKER, provider.transcript(2))
                    self.assertIn("inspect build", provider.transcript(2))
                    self.assertIn("VERIFIED_FILE_EVIDENCE", provider.transcript(3))
                    self.assertFalse(Path(workspace, "unrelated.txt").exists())
                    self.assertEqual(provider.max_in_flight, 1)
                    # The monitor turn was announced, with its origin, but nothing of it streamed.
                    monitor_turns = [s for s in listener.turn_starts(handle.id) if s.get("origin") == "monitor"]
                    self.assertEqual(len(monitor_turns), 1)
                    after_registration = listener.events.index(next(e for e in listener.events if e[1] == "turn.started" and e[2].get("origin") == "monitor"))
                    streamed = [e for _, e, _ in listener.events[after_registration:] if not e.startswith("turn.")]
                    self.assertEqual(streamed, [])
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_journal_failure_after_authorship_retries_without_repeating_the_model(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                provider = _register("continuation", "Verified build result.")
                listener = Listener()
                runtime, handle = await registered_runtime(home, workspace, listener, provider, mode="continuation")
                try:
                    listener.fail_publish = OSError("disk write failed")
                    monitor = await complete(runtime)
                    await settled(runtime)
                    self.assertEqual(listener.notices(handle.id), [])
                    self.assertEqual(provider.answered, 3)
                    listener.fail_publish = None
                    await make_available(monitor["id"])
                    await runtime.sessions.monitors.poll()
                    await eventually(lambda: listener.notices(handle.id), label="publication from the checkpoint")
                    await settled(runtime)
                    self.assertEqual(len(listener.notices(handle.id)), 1)
                    self.assertEqual(listener.notices(handle.id)[0]["text"], "Verified build result.")
                    self.assertEqual(provider.answered, 3)  # the checkpoint was reused, no new model turn
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_busy_user_turn_preserves_completion_and_serializes_continuation(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                provider = _register("continuation", "Foreground finished.", "Inspected completion.")
                listener = Listener()
                runtime, handle = await registered_runtime(home, workspace, listener, provider, mode="continuation")
                try:
                    provider.gate.clear()
                    busy = asyncio.ensure_future(handle.run_turn("Answer this while the task runs"))
                    await eventually(lambda: provider.in_flight == 1, label="the user turn at the provider")
                    await complete(runtime)
                    await asyncio.sleep(0.2)
                    self.assertEqual(listener.notices(handle.id), [])
                    provider.gate.set()
                    await busy
                    await eventually(lambda: listener.notices(handle.id), label="the notice after the user turn")
                    await settled(runtime)
                    self.assertEqual(len(listener.notices(handle.id)), 1)
                    self.assertEqual(provider.max_in_flight, 1)
                    self.assertEqual(provider.answered, 4)
                finally:
                    provider.gate.set()
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_cancellation_while_authoring_fences_publication_and_frees_the_session(self):
        for action in ("mute", "stop"):
            with self.subTest(action=action):
                host_module._ACTIVE = None
                reset_configured_knobs()
                root, home, workspace, previous = self.temp_context()
                try:
                    async def scenario():
                        provider = _register("continuation", "Unverified late result")
                        listener = Listener()
                        runtime, handle = await registered_runtime(home, workspace, listener, provider, mode="continuation")
                        controller = MonitorDeliveryController(runtime.home)
                        try:
                            provider.gate.clear()
                            monitor = await complete(runtime)
                            await eventually(lambda: provider.in_flight == 1, label="the continuation at the provider")
                            if action == "mute":
                                await controller.set_muted(monitor["id"], monitor["sessionId"], True)
                            else:
                                await controller.stop(monitor["id"], monitor["sessionId"])
                            await settled(runtime, timeout=3.0)
                            self.assertEqual(listener.notices(handle.id), [])
                            self.assertEqual(provider.in_flight, 0)
                            if action == "mute":
                                await controller.set_muted(monitor["id"], monitor["sessionId"], False)
                                await runtime.sessions.monitors.poll()
                                await eventually(lambda: listener.notices(handle.id), label="the consolidated update after unmute")
                                await settled(runtime)
                                self.assertEqual(len(listener.notices(handle.id)), 1)
                                self.assertIn("interrupted", listener.notices(handle.id)[0]["text"])
                                self.assertEqual(provider.answered, 2)
                            provider.gate.set()
                            result = await asyncio.wait_for(handle.run_turn("Still responsive?"), 3)
                            self.assertEqual(result["stopReason"], "end_turn")
                        finally:
                            provider.gate.set()
                            await runtime.stop()

                    run(scenario())
                finally:
                    self.restore_home(root, previous)

    def test_restart_after_durable_append_before_engine_ack_is_idempotent(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                import agent_core.tasks.monitor_transactions as transactions

                provider = _register()
                listener = Listener()
                runtime, handle = await registered_runtime(home, workspace, listener, provider)
                original = transactions.save_monitor
                failed = []

                async def crash_ack(record):
                    if record.get("delivery", {}).get("lastReceipt") and not failed:
                        failed.append(True)
                        raise OSError("crash after durable host append")
                    await original(record)

                try:
                    with patch.object(transactions, "save_monitor", crash_ack):
                        monitor = await complete(runtime)
                        await settled(runtime)
                    self.assertEqual(len(listener.notices(handle.id)), 1)
                    first = listener.notices(handle.id)[0]["receipt"]
                finally:
                    await runtime.stop()

                host_module._ACTIVE = None
                await make_available(monitor["id"])
                restarted = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False,
                                                session_options=lambda sid, reason: SessionOptions(provider_override=provider.override), startup_timeout_seconds=20)
                try:
                    await restarted.sessions.monitors.poll()
                    await settled(restarted)
                    self.assertEqual([n["receipt"] for n in listener.notices(handle.id)], [first])
                    self.assertEqual(provider.answered, 2)
                    self.assertEqual((await load_monitor(monitor["id"]))["delivery"]["lastReceipt"]["seq"], first["seq"])
                finally:
                    await restarted.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_hanging_provider_has_the_configured_deadline_and_a_visible_failure(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[DEADLINE], os.environ[CLEANUP] = "1000", "2000"

            async def scenario():
                provider = _register("continuation")
                listener = Listener()
                runtime, handle = await registered_runtime(home, workspace, listener, provider, mode="continuation")
                try:
                    provider.gate.clear()
                    await complete(runtime)
                    await settled(runtime, timeout=3.0)
                    self.assertEqual(provider.in_flight, 0)
                    self.assertEqual(listener.notices(handle.id), [])
                    await runtime.sessions.monitors.poll()
                    self.assertEqual(runtime.status()["sessions"]["monitors"]["failed"], 1)
                finally:
                    provider.gate.set()
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_intake_timer_publishes_without_a_manual_poll_or_another_user_message(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[POLL] = "100"

            async def scenario():
                provider = _register()
                listener = Listener()
                runtime, handle = await registered_runtime(home, workspace, listener, provider)
                try:
                    await complete(runtime, poll=False)
                    await eventually(lambda: any(n["reason"] == "completed" for n in listener.notices(handle.id)), label="the timer's delivery")
                    await settled(runtime)
                    self.assertEqual(sum(n["reason"] == "completed" for n in listener.notices(handle.id)), 1)
                    self.assertEqual(provider.answered, 2)
                    self.assertEqual([s["origin"] for s in listener.turn_starts(handle.id)], ["user"])
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_continuation_disabled_leaves_work_pending_visibly_and_never_downgrades_it(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                provider = _register("continuation", "Inspected.")
                listener = Listener()
                runtime, handle = await registered_runtime(home, workspace, listener, provider, mode="continuation")
                await runtime.stop()

                def options(sid, reason):
                    return SessionOptions(provider_override=provider.override)

                host_module._ACTIVE = None
                disabled = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False, session_options=options, startup_timeout_seconds=20, monitor_continuation=False)
                try:
                    await complete(disabled)
                    await settled(disabled)
                    self.assertEqual(listener.notices(handle.id), [])
                    status = disabled.status()["sessions"]["monitors"]
                    self.assertEqual((status["pending"], status["error"]), (1, "monitor.continuation_disabled"))
                    self.assertEqual(provider.answered, 2)
                finally:
                    await disabled.stop()

                host_module._ACTIVE = None
                enabled = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False, session_options=options, startup_timeout_seconds=20)
                try:
                    await enabled.sessions.monitors.poll()
                    await eventually(lambda: listener.notices(handle.id), label="the continuation once enabled")
                    await settled(enabled)
                    self.assertEqual(listener.notices(handle.id)[0]["mode"], "continuation")
                    self.assertEqual(provider.answered, 3)
                finally:
                    await enabled.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_no_publisher_is_a_visible_reason_and_no_lease_means_the_controllers_maximum(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ.pop(LEASE, None)   # nothing configured for the lease

            async def scenario():
                provider = _register()
                # A host that cannot publish: the delivery waits, visibly, unclaimed.
                silent = SilentListener()
                runtime, handle = await registered_runtime(home, workspace, silent, provider)
                try:
                    monitor = await complete(runtime)
                    await settled(runtime)
                    status = runtime.status()["sessions"]["monitors"]
                    self.assertEqual((status["pending"], status["error"]), (1, "monitor.publisher_missing"))
                    self.assertEqual(runtime.status()["sessions"]["pending"], [])
                finally:
                    await runtime.stop()

                # A host that can: with no lease configured the claim takes the controller's maximum.
                host_module._ACTIVE = None
                listener = Listener()
                capable = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False,
                                              session_options=lambda sid, reason: SessionOptions(provider_override=provider.override), startup_timeout_seconds=20)
                try:
                    await capable.sessions.monitors.poll()
                    await eventually(lambda: listener.notices(handle.id), label="delivery with the maximum lease")
                    await settled(capable)
                    self.assertIsNone(capable.status()["sessions"]["monitors"]["error"])
                    self.assertEqual(len(listener.notices(handle.id)), 1)
                    self.assertEqual((await load_monitor(monitor["id"]))["delivery"]["lastReceipt"]["deliveryId"], listener.notices(handle.id)[0]["deliveryId"])
                finally:
                    await capable.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_session_the_host_refuses_keeps_its_delivery_pending_visibly(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[IDLE] = "200"
            archived = set()

            async def scenario():
                provider = _register()
                listener = Listener()

                def options(session_id, reason):
                    if reason == "monitor" and session_id in archived:
                        return Unavailable("archived")
                    return SessionOptions(provider_override=provider.override)

                runtime, handle = await registered_runtime(home, workspace, listener, provider, options_for=options)
                try:
                    await eventually(lambda: not handle.is_open, label="idle close")
                    archived.add(handle.id)
                    await complete(runtime)
                    await eventually(lambda: any(p["state"] == "refused" for p in runtime.status()["sessions"]["pending"]), label="the refusal")
                    refused = runtime.status()["sessions"]["pending"][0]
                    self.assertEqual((refused["origin"], refused["reason"], refused["sessionId"]), ("monitor", "archived", handle.id))
                    self.assertEqual(listener.notices(handle.id), [])
                    self.assertFalse(handle.is_open)
                    self.assertEqual(runtime.status()["sessions"]["monitors"]["pending"], 1)
                    self.assertEqual([e for e, _ in listener.runtime_events], ["session.reopen_refused"])
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)
