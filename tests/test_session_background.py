"""The rules of a session's queue for turns the runtime starts itself: the
user's turns go first, an aged background turn takes the next slot, a
guarded turn whose work is gone is skipped, and a ceiling exists only when
the configuration sets one (the owner's rule: nothing configured, no
ceiling; never a number chosen in code).
"""
from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core import SessionOptions, Unavailable, start_runtime
from tess_core.config._knobs import reset_configured_knobs
from tess_core.runtime import host as host_module
from tess_core.runtime.session_wake import background_limits

RETRY = "TESS_CORE_SESSIONS_PENDING_RETRY_MS"
FAIR = "TESS_CORE_SESSIONS_BACKGROUND_MAX_WAIT_MS"
CALLS = "TESS_CORE_SESSIONS_BACKGROUND_MAX_PROVIDER_CALLS"


class Listener:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def on_event(self, session_id, envelope):
        self.events.append((session_id, envelope["event"], envelope["data"]))

    def turn_starts(self, session_id):
        return [data for sid, event, data in self.events if sid == session_id and event == "turn.started"]


async def _start(home, workspace, listener, **kwargs):
    return await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False, startup_timeout_seconds=20, **kwargs)


async def _wait(predicate, timeout=20.0, label=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timeout waiting for {label}")


def _provider(order, label, gate=None):
    async def send(_request):
        if gate is not None:
            await gate.wait()
        order.append(label)
        return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {}, "content": [{"type": "text", "text": label}]}

    return {"send": send}


class SessionBackgroundTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def tearDown(self):
        for name in (RETRY, FAIR, CALLS):
            os.environ.pop(name, None)
        reset_configured_knobs()

    def test_user_turns_go_first_and_an_aged_background_turn_takes_the_next_slot(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[FAIR] = "400"

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                try:
                    handle = await runtime.sessions.open()
                    live = runtime.sessions.live(handle.id)
                    order = []

                    gate = asyncio.Event()
                    t0 = asyncio.ensure_future(handle.run_turn("t0", turn_id="t0", provider_override=_provider(order, "t0", gate)))
                    await _wait(lambda: handle.turns()["running"], label="t0 running")
                    bg1 = asyncio.ensure_future(live.turns.run("", origin="child_message", turn_id="bg1", provider_override=_provider(order, "bg1")))
                    await asyncio.sleep(0.05)
                    t1 = asyncio.ensure_future(handle.run_turn("t1", turn_id="t1", provider_override=_provider(order, "t1")))
                    await _wait(lambda: len(handle.turns()["queued"]) == 2, label="two queued")
                    gate.set()
                    await asyncio.gather(t0, bg1, t1)
                    self.assertEqual(order, ["t0", "t1", "bg1"])   # the user's turn first, though it arrived later
                    self.assertEqual([s["origin"] for s in listener.turn_starts(handle.id)], ["user", "user", "child_message"])

                    order.clear()
                    gate = asyncio.Event()
                    t2 = asyncio.ensure_future(handle.run_turn("t2", turn_id="t2", provider_override=_provider(order, "t2", gate)))
                    await _wait(lambda: handle.turns()["running"], label="t2 running")
                    bg2 = asyncio.ensure_future(live.turns.run("", origin="child_message", turn_id="bg2", provider_override=_provider(order, "bg2")))
                    await asyncio.sleep(0.5)   # past the fairness limit
                    t3 = asyncio.ensure_future(handle.run_turn("t3", turn_id="t3", provider_override=_provider(order, "t3")))
                    await _wait(lambda: len(handle.turns()["queued"]) == 2, label="two queued again")
                    self.assertGreaterEqual(handle.turns()["queued"][0]["waitedSeconds"], 0.4)
                    gate.set()
                    await asyncio.gather(t2, bg2, t3)
                    self.assertEqual(order, ["t2", "bg2", "t3"])   # the aged background turn took the slot

                    # A guarded background turn whose work is gone is skipped, never started.
                    starts = len(listener.turn_starts(handle.id))
                    skipped = await live.turns.run("", origin="child_message", turn_id="bg3", provider_override=_provider(order, "bg3"), only_if=lambda: False)
                    self.assertEqual(skipped["stopReason"], "skipped")
                    self.assertEqual(len(listener.turn_starts(handle.id)), starts)
                    self.assertNotIn("bg3", order)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_nothing_configured_means_no_ceiling_and_no_retry_delay(self):
        root, home, workspace, previous = self.temp_context()
        try:
            self.assertIsNone(background_limits())
            os.environ[CALLS] = "2"
            self.assertEqual(background_limits(), {"maxProviderCalls": 2})
            os.environ[CALLS] = "null"
            self.assertIsNone(background_limits())
            os.environ.pop(CALLS)

            refusals = []

            def options_for(session_id, reason):
                if reason == "child_message":
                    refusals.append(session_id)
                    return Unavailable("later", retry=True)
                return SessionOptions()

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, session_options=options_for)
                try:
                    parent = await runtime.sessions.open()
                    await runtime.sessions.close(parent.id)
                    announcement = {"event": "subagent.message", "sessionId": parent.id, "data": {"task_id": "t", "message_id": "m", "parent_session_id": parent.id}}
                    for _ in range(3):
                        runtime.sessions.route_daemon_event(announcement)
                        await _wait(lambda: runtime.status()["sessions"]["pending"][0]["state"] == "waiting", label="a refusal")
                    self.assertEqual(len(refusals), 3)   # no delay: every announcement is offered to the host again
                    self.assertIsNone(runtime.status()["sessions"]["pending"][0]["nextRetryAt"])

                    os.environ[RETRY] = "60000"
                    runtime.sessions.route_daemon_event(announcement)
                    await _wait(lambda: len(refusals) == 4, label="a refusal with a configured delay")
                    self.assertTrue(runtime.status()["sessions"]["pending"][0]["nextRetryAt"])
                    runtime.sessions.route_daemon_event(announcement)
                    await asyncio.sleep(0.1)
                    self.assertEqual(len(refusals), 4)   # within the delay: not offered again
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)
