"""Turns inside a session: strictly one at a time per session, concurrent
across sessions, named by id, cancellable by that id only. The provider is a
fake per turn (``provider_override``); everything else is the real engine.

Every test is one coroutine: a runtime belongs to the loop that started it.
"""
from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core import SessionBusy, SessionOptions, start_runtime
from tess_core.runtime import host as host_module


class Listener:
    def __init__(self, answer="yes") -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.questions: list[tuple[str, str, list]] = []
        self.closed: list[tuple[str, str]] = []
        self.answer = answer

    def on_event(self, session_id, envelope):
        self.events.append((session_id, envelope["event"], envelope))

    def on_session_closed(self, session_id, reason):
        self.closed.append((session_id, reason))

    async def ask_question(self, session_id, question, options, detail):
        self.questions.append((session_id, question, list(options or [])))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def turn_events(self, session_id):
        """(event, turn_id) for the turn lifecycle of one session, in order."""
        out = []
        for sid, event, envelope in self.events:
            if sid == session_id and event.startswith("turn."):
                out.append((event, envelope["data"].get("turn_id")))
        return out


def _reply(text="ok"):
    return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": text}]}


def _tool_use(name, input, use_id="use_1"):
    return {"model": "gpt-5.4", "stopReason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "tool_use", "id": use_id, "name": name, "input": input}]}


def _provider(log, label, *, gate=None, delay=0.0, reply=None):
    """A fake provider: logs start/end under a label, optionally waits."""

    async def send(request):
        log.append(("start", label, time.monotonic()))
        if gate is not None:
            await gate.wait()
        if delay:
            await asyncio.sleep(delay)
        log.append(("end", label, time.monotonic()))
        return reply or _reply(label)

    return {"send": send}


async def _start(home, workspace, listener, **kwargs):
    return await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False, startup_timeout_seconds=20, **kwargs)


async def _wait(predicate, timeout=10.0, label=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timeout waiting for {label}")


class SessionTurnTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def test_turns_on_one_session_run_in_order_while_sessions_run_concurrently(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                try:
                    alpha = await runtime.sessions.open()
                    bravo = await runtime.sessions.open()
                    log = []
                    results = await asyncio.gather(
                        alpha.run_turn("a1", provider_override=_provider(log, "a1", delay=0.3)),
                        alpha.run_turn("a2", provider_override=_provider(log, "a2", delay=0.1)),
                        bravo.run_turn("b1", provider_override=_provider(log, "b1", delay=0.1)),
                    )
                    self.assertEqual([r["text"] for r in results], ["a1", "a2", "b1"])
                    at = {(kind, label): t for kind, label, t in log}
                    self.assertLess(at[("end", "a1")], at[("start", "a2")])   # strictly one at a time in alpha
                    self.assertLess(at[("start", "b1")], at[("end", "a1")])   # bravo did not wait for alpha

                    alpha_turns = listener.turn_events(alpha.id)
                    ids = [turn_id for event, turn_id in alpha_turns if event == "turn.started"]
                    self.assertEqual(len(ids), 2)
                    self.assertNotEqual(ids[0], ids[1])
                    self.assertEqual(
                        alpha_turns,
                        [("turn.started", ids[0]), ("turn.completed", None), ("turn.started", ids[1]), ("turn.completed", None)],
                    )
                    started = next(e for sid, ev, e in listener.events if sid == alpha.id and ev == "turn.started")
                    self.assertEqual(started["data"]["origin"], "user")
                    self.assertEqual(started["typedEvent"]["payload"], {"prompt": "a1", "model": "gpt-5.4", "agentName": None, "turnId": ids[0], "origin": "user"})
                    # A client's own startTurn keeps the payload it always had.
                    legacy = core.create_runtime_event({"event": "turn.started", "data": {"session_id": "s", "prompt": "go"}})
                    self.assertEqual(legacy["payload"], {"prompt": "go", "model": None, "agentName": None})
                    self.assertEqual(alpha.message_count, 4)
                    self.assertEqual(runtime.status()["sessions"]["open"][0]["turn"], None)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_cancel_hits_only_the_named_turn(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                try:
                    handle = await runtime.sessions.open()
                    log, never = [], asyncio.Event()
                    first = asyncio.ensure_future(handle.run_turn("slow", turn_id="t-slow", provider_override=_provider(log, "slow", gate=never)))
                    second = asyncio.ensure_future(handle.run_turn("next", turn_id="t-next", provider_override=_provider(log, "next")))
                    await _wait(lambda: any(kind == "start" for kind, *_ in log), label="first turn at the provider")
                    self.assertEqual(handle.turns()["running"]["id"], "t-slow")
                    self.assertEqual([t["id"] for t in handle.turns()["queued"]], ["t-next"])
                    self.assertEqual(runtime.status()["sessions"]["open"][0]["queued"], 1)

                    self.assertTrue(await handle.cancel_turn("t-slow"))
                    self.assertEqual((await first)["stopReason"], "cancelled")
                    self.assertEqual((await second)["stopReason"], "end_turn")   # the next turn was untouched
                    self.assertFalse(await handle.cancel_turn("t-slow"))       # finished: a no-op
                    self.assertFalse(await handle.cancel_turn("nope"))         # unknown: a no-op
                    self.assertEqual(
                        listener.turn_events(handle.id),
                        [("turn.started", "t-slow"), ("turn.cancelling", None), ("turn.cancelled", None), ("turn.started", "t-next"), ("turn.completed", None)],
                    )
                    self.assertFalse(handle.busy)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_queued_turn_is_cancelled_before_it_starts(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                try:
                    handle = await runtime.sessions.open()
                    log, gate = [], asyncio.Event()
                    first = asyncio.ensure_future(handle.run_turn("slow", turn_id="t-slow", provider_override=_provider(log, "slow", gate=gate)))
                    queued = asyncio.ensure_future(handle.run_turn("queued", turn_id="t-queued", provider_override=_provider(log, "queued")))
                    await _wait(lambda: handle.turns()["queued"], label="second turn queued")
                    self.assertTrue(await handle.cancel_turn("t-queued"))
                    cancelled = await asyncio.wait_for(queued, 5)     # resolved at once, the first turn still runs
                    self.assertEqual((cancelled["stopReason"], cancelled["text"], cancelled["toolCount"]), ("cancelled", "", 0))
                    self.assertEqual(handle.turns()["running"]["id"], "t-slow")
                    gate.set()
                    self.assertEqual((await first)["text"], "slow")
                    self.assertEqual([label for kind, label, _ in log if kind == "start"], ["slow"])  # never reached the provider
                    self.assertEqual(listener.turn_events(handle.id), [("turn.started", "t-slow"), ("turn.completed", None)])
                    with self.assertRaises(ValueError):   # an id already queued or running is refused
                        await self._duplicate(handle, log)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    async def _duplicate(self, handle, log):
        gate = asyncio.Event()
        running = asyncio.ensure_future(handle.run_turn("one", turn_id="same", provider_override=_provider(log, "one", gate=gate)))
        await _wait(lambda: handle.turns()["running"], label="turn running")
        try:
            await handle.run_turn("two", turn_id="same", provider_override=_provider(log, "two"))
        finally:
            gate.set()
            await running

    def test_close_is_refused_while_busy_and_a_handle_reopens_its_session(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                try:
                    handle = await runtime.sessions.open()
                    log, gate = [], asyncio.Event()
                    running = asyncio.ensure_future(handle.run_turn("slow", turn_id="t-slow", provider_override=_provider(log, "slow", gate=gate)))
                    await _wait(lambda: handle.turns()["running"], label="turn running")
                    with self.assertRaises(SessionBusy):
                        await runtime.sessions.close(handle.id)
                    self.assertTrue(handle.is_open)
                    self.assertEqual(listener.closed, [])
                    gate.set()
                    await running
                    await runtime.sessions.close(handle.id)
                    self.assertFalse(handle.is_open)

                    # The same handle, closed: run_turn reopens the session and the record continues.
                    result = await handle.run_turn("after reopen", provider_override=_provider(log, "again"))
                    self.assertEqual(result["text"], "again")
                    self.assertTrue(handle.is_open)
                    self.assertEqual(handle.message_count, 4)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_questions_reach_the_host_with_the_session_id(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener(answer="yes")
                runtime = await _start(home, workspace, listener)
                try:
                    handle = await runtime.sessions.open()
                    calls = {"n": 0}

                    async def asker(_request):
                        calls["n"] += 1
                        if calls["n"] == 1:
                            return _tool_use("AskUserQuestion", {"questions": [{"question": "Deploy?", "options": [{"label": "yes"}, {"label": "no"}]}]})
                        return _reply("done")

                    result = await handle.run_turn("ask me", provider_override={"send": asker})
                    self.assertEqual(result["text"], "done")
                    self.assertEqual(listener.questions, [(handle.id, "Deploy?", ["yes", "no"])])
                    live = runtime.sessions.live(handle.id)
                    tool_results = [b for m in live.record["messages"] if isinstance(m.get("content"), list) for b in m["content"] if b.get("type") == "tool_result"]
                    self.assertFalse(tool_results[-1]["is_error"])
                    self.assertIn('"answer": "yes"', tool_results[-1]["content"])

                    # The host decides what a timeout means: here it raises. The turn
                    # does not wedge the session either way.
                    listener.answer = TimeoutError("host gave up")
                    calls["n"] = 0
                    outcome = None
                    try:
                        outcome = await handle.run_turn("ask again", provider_override={"send": asker})
                    except TimeoutError:
                        pass
                    self.assertFalse(handle.busy)
                    self.assertEqual((await handle.run_turn("still alive", provider_override=_provider([], "alive")))["text"], "alive")
                    if outcome is not None:
                        self.assertEqual(outcome["text"], "done")
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_failing_turn_is_recorded_and_the_session_stays_usable(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                try:
                    handle = await runtime.sessions.open()

                    async def boom(_request):
                        raise RuntimeError("provider exploded")

                    with self.assertRaises(RuntimeError) as failed:
                        await handle.run_turn("fail", turn_id="t-fail", provider_override={"send": boom})
                    self.assertEqual(str(failed.exception), "provider exploded")
                    self.assertEqual(listener.turn_events(handle.id), [("turn.started", "t-fail"), ("turn.failed", None)])
                    failed_event = next(e for sid, ev, e in listener.events if ev == "turn.failed")
                    self.assertEqual(failed_event["data"]["message"], "provider exploded")
                    self.assertFalse(handle.busy)
                    self.assertTrue(handle.is_open)
                    self.assertEqual((await handle.run_turn("again", provider_override=_provider([], "fine")))["text"], "fine")
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_per_turn_context_reaches_the_provider_but_never_the_transcript(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                contexts = iter(["EPHEMERAL-ONE", None, "EPHEMERAL-THREE"])

                async def context_provider(session_id):
                    return next(contexts)

                runtime = await _start(home, workspace, listener, session_options=lambda sid, reason: SessionOptions(context_provider=context_provider))
                try:
                    handle = await runtime.sessions.open()
                    seen = []

                    async def send(request):
                        seen.append(request["system"])
                        return _reply("ok")

                    for prompt in ("one", "two", "three"):
                        await handle.run_turn(prompt, provider_override={"send": send})
                    self.assertIn("EPHEMERAL-ONE", seen[0])
                    self.assertNotIn("EPHEMERAL", seen[1])
                    self.assertIn("EPHEMERAL-THREE", seen[2])
                    self.assertNotIn("EPHEMERAL-ONE", seen[2])
                    live = runtime.sessions.live(handle.id)
                    self.assertNotIn("EPHEMERAL", json.dumps(live.record["messages"]))
                    self.assertNotIn("EPHEMERAL", live.record.get("systemPrompt") or "")
                    stored = await core.load_session(handle.id)
                    self.assertNotIn("EPHEMERAL", json.dumps(stored))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_turn_limits_bound_the_turn(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                try:
                    handle = await runtime.sessions.open()
                    calls = {"n": 0}

                    async def endless(_request):
                        calls["n"] += 1
                        return _tool_use("Glob", {"pattern": "*.nothing"}, use_id=f"use_{calls['n']}")

                    result = await handle.run_turn("loop forever", limits={"maxProviderCalls": 2}, provider_override={"send": endless})
                    self.assertNotEqual(result["stopReason"], "end_turn")
                    self.assertLessEqual(calls["n"], 2)
                    self.assertFalse(handle.busy)
                    with self.assertRaises(ValueError):
                        await handle.run_turn("bad limits", limits={"bogus": 1}, provider_override={"send": endless})
                    self.assertFalse(handle.busy)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_stop_lets_a_running_turn_finish_within_the_grace_then_aborts(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                handle = await runtime.sessions.open()
                log, gate = [], asyncio.Event()
                running = asyncio.ensure_future(handle.run_turn("finishes", turn_id="t-finish", provider_override=_provider(log, "finish", delay=0.3)))
                queued = asyncio.ensure_future(handle.run_turn("never", turn_id="t-never", provider_override=_provider(log, "never")))
                await _wait(lambda: handle.turns()["running"], label="turn running")
                await runtime.stop(grace=10)
                self.assertEqual((await running)["stopReason"], "end_turn")      # finished inside the grace
                self.assertEqual((await queued)["stopReason"], "cancelled")      # never started
                self.assertEqual([label for kind, label, _ in log if kind == "start"], ["finish"])
                self.assertEqual(listener.closed, [(handle.id, "runtime_stopped")])

                host_module._ACTIVE = None
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                handle = await runtime.sessions.open()
                stuck = asyncio.ensure_future(handle.run_turn("stuck", turn_id="t-stuck", provider_override=_provider(log, "stuck", gate=gate)))
                await _wait(lambda: handle.turns()["running"], label="turn running")
                started = time.monotonic()
                await runtime.stop(grace=0.2)
                self.assertEqual((await stuck)["stopReason"], "cancelled")       # aborted after the grace
                self.assertLess(time.monotonic() - started, 5.0)
                self.assertIn(("turn.cancelling", None), listener.turn_events(handle.id))

            run(scenario())
        finally:
            self.restore_home(root, previous)
