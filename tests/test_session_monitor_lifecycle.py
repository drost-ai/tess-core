"""One total deadline per monitor delivery, quarantine of unfinished cleanup,
and retained prior work: the host's lifecycle matrix (tess-agent
tests/test_monitor_lifecycle.py and the cancellation-resistant case of
test_monitor_delivery.py) ported against the runtime.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

from tests.support.agent_core_case import *
from tests.support.session_monitors import (
    CLEANUP, DEADLINE, LEASE, MONITOR_ENV, Listener, ScriptedProvider, complete, eventually, registered_runtime, settled, tool_reply,
)

from agent_core import MonitorDeliveryController
from agent_core.config._knobs import reset_configured_knobs
from agent_core.runtime import host as host_module
from agent_core.storage import list_monitors, load_monitor


def _register(mode, *more):
    fields = {"task_id": "build", "goal": "watch" if mode == "notification" else "inspect"}
    if mode == "continuation":
        fields["delivery_mode"] = "continuation"
    return ScriptedProvider(tool_reply("TaskMonitorStart", fields), "Registered.", *more)


class SessionMonitorLifecycleTests(AgentCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None
        os.environ[LEASE] = "120000"
        os.environ[DEADLINE], os.environ[CLEANUP] = "1000", "2000"

    def tearDown(self):
        for name in MONITOR_ENV:
            os.environ.pop(name, None)
        reset_configured_knobs()

    def test_total_deadline_covers_storage_and_quarantines_unfinished_cleanup(self):
        for phase in ("claim", "retry", "renew"):
            for resistant in (False, True):
                with self.subTest(phase=phase, resistant=resistant):
                    host_module._ACTIVE = None
                    reset_configured_knobs()
                    root, home, workspace, previous = self.temp_context()
                    try:
                        async def scenario():
                            entered, release = asyncio.Event(), asyncio.Event()
                            mode = "continuation" if phase in {"claim", "renew"} else "notification"
                            provider = _register(mode, "Late inspection")
                            listener = Listener()
                            runtime, handle = await registered_runtime(home, workspace, listener, provider, mode=mode)
                            controller = runtime.sessions.monitors.controller
                            original = getattr(controller, phase)

                            async def stalled(*args, **kwargs):
                                entered.set()
                                while not release.is_set():
                                    try:
                                        await release.wait()
                                    except asyncio.CancelledError:
                                        if not resistant:
                                            raise
                                return await original(*args, **kwargs)

                            try:
                                with ExitStack() as patches:
                                    patches.enter_context(patch.object(controller, phase, stalled))
                                    if phase == "retry":
                                        patches.enter_context(patch.object(controller, "checkpoint", side_effect=OSError("checkpoint unavailable")))
                                    if phase == "renew":
                                        provider.gate.clear()
                                    started = asyncio.get_running_loop().time()
                                    monitor = await complete(runtime)
                                    await asyncio.wait_for(entered.wait(), 1.5)
                                    await settled(runtime, timeout=1.5)
                                    self.assertLess(asyncio.get_running_loop().time() - started, 1.75)
                                    self.assertEqual(listener.notices(handle.id), [])
                                    live = runtime.sessions.live(handle.id)
                                    if resistant:
                                        self.assertTrue(live.monitor_cleanup_pending)
                                        self.assertTrue(runtime.status()["sessions"]["open"][0]["monitorCleanupPending"])
                                        during = asyncio.ensure_future(handle.run_turn("Keep serialized"))
                                        await asyncio.sleep(0.3)
                                        self.assertFalse(during.done())  # quarantined: nothing runs until the cleanup settles
                                        self.assertEqual(provider.answered, 2)
                                    release.set()
                                    provider.gate.set()
                                    await eventually(lambda: not live.monitor_cleanup_pending, label="cleanup settled")
                                    if resistant:
                                        await asyncio.wait_for(during, 3)
                                        self.assertEqual(provider.answered, 3)
                                    self.assertEqual(listener.notices(handle.id), [])
                                    record = await load_monitor(monitor["id"])
                                    if phase == "claim":
                                        self.assertFalse(record["delivery"]["queue"][0].get("executionStarted"))  # a late claim launched nothing
                                    if phase == "renew":
                                        self.assertEqual(record["delivery"]["queue"][0]["state"], "failed")
                            finally:
                                release.set()
                                provider.gate.set()
                                await runtime.stop()

                        run(scenario())
                    finally:
                        self.restore_home(root, previous)

    def test_late_publication_callback_is_fenced_after_the_deadline(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                entered, release = asyncio.Event(), asyncio.Event()
                provider = _register("notification")
                listener = Listener()
                runtime, handle = await registered_runtime(home, workspace, listener, provider)
                controller = runtime.sessions.monitors.controller

                async def stalled_publish(claim, session, config, publisher):
                    entered.set()
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            pass
                    return await publisher({**claim["envelope"], "text": "Late unchecked append"})

                try:
                    with patch.object(controller, "publish_to_session", stalled_publish):
                        await complete(runtime)
                        await asyncio.wait_for(entered.wait(), 1)
                        await settled(runtime, timeout=1.5)
                        live = runtime.sessions.live(handle.id)
                        self.assertTrue(live.monitor_cleanup_pending)
                        release.set()
                        await eventually(lambda: not live.monitor_cleanup_pending, label="cleanup settled")
                        self.assertEqual(listener.notices(handle.id), [])  # the late callback was fenced
                finally:
                    release.set()
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_cancellation_resistant_turn_has_bounded_cleanup_and_cannot_overlap_the_next_turn(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                import agent_core.runtime.session_turns as turns_module

                release = asyncio.Event()
                original = turns_module.run_agent_prompt
                provider = _register("continuation", "Responsive.")
                listener = Listener()
                runtime, handle = await registered_runtime(home, workspace, listener, provider, mode="continuation")
                live = runtime.sessions.live(handle.id)

                async def resistant_prompt(request):
                    if request.get("monitorEnvelope") is None:
                        return await original(request)
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            pass
                    live.publish("message.assistant", {"text": "Late result"})
                    return {"text": "Late result", "stopReason": "end_turn"}

                try:
                    with patch.object(turns_module, "run_agent_prompt", resistant_prompt):
                        await complete(runtime)
                        await settled(runtime, timeout=4.5)
                        self.assertTrue(live.monitor_cleanup_pending)
                        self.assertEqual(listener.notices(handle.id), [])
                        during = asyncio.ensure_future(handle.run_turn("Do not overlap"))
                        await asyncio.sleep(0.3)
                        self.assertFalse(during.done())
                        self.assertEqual(provider.answered, 2)
                        release.set()
                        await eventually(lambda: not live.monitor_cleanup_pending, label="cleanup settled")
                        self.assertEqual(listener.notices(handle.id), [])
                        self.assertFalse(any(d.get("text") == "Late result" for _, e, d in listener.events if e == "message.assistant"))
                        await asyncio.wait_for(during, 3)
                        self.assertEqual(provider.answered, 3)
                finally:
                    release.set()
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_latest_completion_retains_prior_work_without_another_model_turn(self):
        for phase in ("executing", "authored"):
            with self.subTest(phase=phase):
                host_module._ACTIVE = None
                reset_configured_knobs()
                os.environ.pop(DEADLINE, None)
                os.environ.pop(CLEANUP, None)
                root, home, workspace, previous = self.temp_context()
                try:
                    async def scenario():
                        provider = _register("continuation", "Earlier inspected progress")
                        listener = Listener()
                        runtime, handle = await registered_runtime(home, workspace, listener, provider, mode="continuation")
                        controller = MonitorDeliveryController(runtime.home)
                        try:
                            if phase == "executing":
                                provider.gate.clear()
                                await runtime.sessions.monitors.poll()
                                await eventually(lambda: provider.in_flight == 1, label="the first continuation at the provider")
                                monitor = await complete(runtime)
                                await controller.set_muted(monitor["id"], monitor["sessionId"], True)
                            else:
                                listener.fail_publish = OSError("journal unavailable")
                                await runtime.sessions.monitors.poll()
                                await settled(runtime)
                                monitor = (await list_monitors())[0]
                                await controller.set_muted(monitor["id"], monitor["sessionId"], True)
                                listener.fail_publish = None
                                await complete(runtime)
                            await settled(runtime, timeout=3.0)
                            calls = provider.answered
                            self.assertEqual(listener.notices(handle.id), [])
                            held = await load_monitor(monitor["id"])
                            history = held["delivery"]["interruption"]["deliveries"]
                            self.assertEqual(len(history), 1)
                            self.assertTrue(history[0]["executionStarted"])
                            if phase == "authored":
                                self.assertEqual(history[0]["result"]["text"], "Earlier inspected progress")
                            await controller.set_muted(monitor["id"], monitor["sessionId"], False)
                            provider.gate.set()
                            await runtime.sessions.monitors.poll()
                            await eventually(lambda: listener.notices(handle.id), label="the consolidated update")
                            await settled(runtime)
                            updates = listener.notices(handle.id)
                            self.assertEqual(len(updates), 1)
                            self.assertEqual(updates[0]["reason"], "completed")
                            self.assertIn("paused", updates[0]["text"])
                            self.assertEqual(provider.answered, calls)
                            delivered = await load_monitor(monitor["id"])
                            self.assertEqual(delivered["delivery"]["interruption"]["deliveries"], history)
                        finally:
                            provider.gate.set()
                            await runtime.stop()

                    run(scenario())
                finally:
                    self.restore_home(root, previous)
