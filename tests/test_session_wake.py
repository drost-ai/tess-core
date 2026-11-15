"""Pending child work reopens a closed parent: runtime-wide discovery through
the daemon's level-triggered announcements, the host's veto, and the engine's
claim semantics untouched. The queue's ordering rules and the no-ceiling
rule live in test_session_background.py.

Real daemon processes where the announcement path is the claim; a real child
worker process for the end-to-end case (its provider profile has no key, so
its first turn fails fast and publishes a result to the parent's mailbox).
Every test is one coroutine: a runtime belongs to the loop that started it.
"""
from __future__ import annotations

from tests.support.tess_core_case import *
from tests.support.session_wake import *
from tests.support.session_wake import _ENV, _adopt_on_disk, _seed_task, _start, _wait

from tess_core import SessionOptions, Unavailable
from tess_core.config._knobs import reset_configured_knobs
from tess_core.runtime import host as host_module
from tess_core.tasks.communication import read_task_mailbox_messages, read_task_questions, send_task_message


class SessionWakeTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def tearDown(self):
        for name in _ENV:
            os.environ.pop(name, None)
        reset_configured_knobs()

    def test_a_child_result_reopens_the_closed_parent_and_is_delivered_once(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[IDLE], os.environ[WATCH] = "600", "200"
            settings_path = home / "settings.json"
            settings_path.write_text(json.dumps({
                "provider": "wake-openai",
                "providerProfiles": {"wake-openai": {"kind": "openai", "defaultModel": "wake-model", "auth": {"kind": "openai_api_key", "envVar": "TESS_CORE_TEST_MISSING_WAKE_KEY"}}},
            }), encoding="utf-8")
            provider = Provider("noted, child")

            def options_for(session_id, reason):
                return SessionOptions(provider="wake-openai", settings=str(settings_path), permission_mode="bypassPermissions", provider_override=provider.override)

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, session_options=options_for)
                try:
                    parent = await runtime.sessions.open()
                    # A parent that spawned a child has a conversation; a transcript
                    # of nothing but synthetic notifications is never persisted.
                    await parent.run_turn("start a child, please")
                    live = runtime.sessions.live(parent.id)
                    config = live.config
                    child = core.create_empty_session(
                        cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"],
                        effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions",
                    )
                    child["messages"].append({"role": "user", "content": "report in"})
                    child["metadata"].update({"parentSessionId": parent.id, "agentId": "wake-agent", "tessCoreSettingsPath": str(settings_path)})
                    child["metadata"]["effectiveAgentAuthority"] = await core.capture_agent_authority(config=config, session=child, tools=core.create_builtin_tools())
                    await core.save_session(child)
                    await core.save_child_agent({
                        "agentId": "wake-agent", "sessionId": child["id"], "parentSessionId": parent.id, "name": "Wake", "provider": config["provider"],
                        "model": config["model"], "mode": "bypassPermissions", "runInBackground": True, "status": "backgrounded",
                        "startedAt": "2026-01-01T00:00:00Z", "effectiveAgentAuthority": child["metadata"]["effectiveAgentAuthority"],
                    })
                    daemon = live.bindings["daemon"]["client"]
                    started = await daemon["startSubagentTask"]({"parentSessionId": parent.id, "sessionId": child["id"], "agentId": "wake-agent", "cwd": str(workspace), "label": "Wake"})
                    task_id = started["taskId"]

                    await _wait(lambda: (parent.id, "idle") in listener.closed, label="the parent's idle close")
                    await _wait(lambda: (parent.id, "child_message") in listener.opened, timeout=40, label="reopen for the child's message")
                    await _wait(lambda: provider.requests, label="the continuity turn's provider call")
                    await _wait(lambda: not runtime.status()["sessions"]["pending"], label="pending work done")

                    starts = [s for s in listener.turn_starts(parent.id) if s.get("origin") == "child_message"]
                    self.assertEqual(len(starts), 1)
                    self.assertTrue(starts[0]["turn_id"])
                    results = [m for m in await read_task_mailbox_messages(task_id) if m.get("direction") == "to_parent"]
                    self.assertTrue(results)
                    self.assertEqual({m["status"] for m in results}, {"delivered"})
                    stored = await core.load_session(parent.id)
                    delivered = [m for m in stored["messages"] if json.dumps(m).count(results[0]["messageId"])]
                    self.assertEqual(len(delivered), 1)   # in the transcript exactly once
                    self.assertEqual(len(provider.requests), 2)   # the user turn, then the continuity turn
                    self.assertIn("start a child, please", provider.transcripts()[1])   # the transcript continued
                    self.assertEqual(listener.runtime_events, [])
                    # The parent goes back to sleep; the claimed message is never announced again.
                    await _wait(lambda: listener.closed.count((parent.id, "idle")) >= 2, label="second idle close")
                    await asyncio.sleep(1.0)
                    self.assertEqual(len(provider.requests), 2)
                    self.assertEqual(listener.opened.count((parent.id, "child_message")), 1)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_unavailable_defers_shows_in_status_and_succeeds_when_the_host_allows(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[IDLE], os.environ[WATCH], os.environ[RETRY] = "300", "200", "300"
            provider = Provider()
            allow = {"child": False}
            asked = []

            def options_for(session_id, reason):
                asked.append(reason)
                if reason == "child_message" and not allow["child"]:
                    return Unavailable("archived", retry=True)
                return SessionOptions(provider_override=provider.override)

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, session_options=options_for)
                try:
                    parent = await runtime.sessions.open()
                    await runtime.sessions.close(parent.id)
                    await _seed_task(workspace, parent.id, "task-deferred", message="child says: done")
                    await _adopt_on_disk(parent.id, "task-deferred")

                    def pending():
                        return runtime.status()["sessions"]["pending"]

                    await _wait(lambda: pending() and pending()[0]["attempts"] >= 2, label="two refused attempts")
                    item = pending()[0]
                    self.assertEqual((item["sessionId"], item["origin"], item["state"], item["reason"], item["retry"]), (parent.id, "child_message", "waiting", "archived", True))
                    self.assertTrue(item["nextRetryAt"])
                    self.assertEqual(item["last"]["event"], "subagent.message")
                    self.assertFalse(parent.is_open)
                    self.assertEqual([e for e, _ in listener.runtime_events], ["session.reopen_refused"] * item["attempts"])
                    self.assertEqual({m["status"] for m in await read_task_mailbox_messages("task-deferred")}, {"queued"})  # still on disk, unclaimed

                    allow["child"] = True
                    await _wait(lambda: (parent.id, "child_message") in listener.opened, label="reopen once allowed")
                    await _wait(lambda: not pending(), label="pending work done")
                    self.assertEqual(len(provider.requests), 1)
                    self.assertIn("child says: done", provider.transcripts()[0])
                    self.assertEqual({m["status"] for m in await read_task_mailbox_messages("task-deferred")}, {"delivered"})
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_repeated_announcements_queue_one_continuity_and_a_drained_mailbox_skips_it(self):
        root, home, workspace, previous = self.temp_context()
        try:
            provider = Provider("seen it")

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, daemon_enabled=False, session_options=lambda sid, reason: SessionOptions(provider_override=provider.override))
                try:
                    handle = await runtime.sessions.open()
                    live = runtime.sessions.live(handle.id)
                    await _seed_task(workspace, handle.id, "task-repeat", message="child result 42")
                    live.record["metadata"]["taskIds"] = ["task-repeat"]
                    await core.persist_session(live.record, live.config)
                    announcement = {"event": "subagent.message", "sessionId": handle.id, "data": {"task_id": "task-repeat", "message_id": "m1", "announcement": "first", "parent_session_id": handle.id, "session_id": handle.id}}
                    for _ in range(5):
                        runtime.sessions.route_daemon_event(announcement)
                    self.assertEqual(len(runtime.status()["sessions"]["pending"]), 1)
                    await _wait(lambda: not runtime.status()["sessions"]["pending"], label="the continuity turn")
                    self.assertEqual(len(provider.requests), 1)
                    self.assertEqual(provider.transcripts()[0].count("child result 42"), 1)
                    starts = listener.turn_starts(handle.id)
                    self.assertEqual([s["origin"] for s in starts], ["child_message"])
                    self.assertEqual({m["status"] for m in await read_task_mailbox_messages("task-repeat")}, {"delivered"})

                    # Repeats for an already claimed message: the continuity is queued and skipped, no provider call.
                    for _ in range(3):
                        runtime.sessions.route_daemon_event({**announcement, "data": {**announcement["data"], "announcement": "repeat"}})
                    await _wait(lambda: not runtime.status()["sessions"]["pending"], label="skipped continuity")
                    self.assertEqual(len(provider.requests), 1)
                    self.assertEqual(len(listener.turn_starts(handle.id)), 1)

                    # A waiting user turn covers a new message: no second continuity is queued.
                    gate = asyncio.Event()

                    async def blocked(_request):
                        await gate.wait()
                        return await provider.send(_request)

                    running = asyncio.ensure_future(handle.run_turn("busy", turn_id="busy", provider_override={"send": blocked}))
                    await _wait(lambda: handle.turns()["running"], label="a running turn")
                    queued_user = asyncio.ensure_future(handle.run_turn("after", turn_id="after"))
                    await _wait(lambda: handle.turns()["queued"], label="a queued user turn")
                    await send_task_message("task-repeat", {"direction": "to_parent", "content": "second result", "kind": "result", "parentSessionId": handle.id})
                    runtime.sessions.route_daemon_event({**announcement, "data": {**announcement["data"], "message_id": "m2"}})
                    self.assertEqual(runtime.status()["sessions"]["pending"], [])
                    gate.set()
                    await asyncio.gather(running, queued_user)
                    self.assertEqual(provider.transcripts()[-1].count("second result"), 1)   # the user turn's drain claimed it
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_failure_after_the_claim_loses_nothing_and_repeats_nothing(self):
        root, home, workspace, previous = self.temp_context()
        try:
            provider = Provider(fail=True)

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, daemon_enabled=False, session_options=lambda sid, reason: SessionOptions(provider_override=provider.override))
                try:
                    handle = await runtime.sessions.open()
                    provider.fail = False
                    await handle.run_turn("start a child, please")   # a conversation exists before the child reports
                    provider.fail = True
                    live = runtime.sessions.live(handle.id)
                    await _seed_task(workspace, handle.id, "task-crash", message="child result before the crash")
                    live.record["metadata"]["taskIds"] = ["task-crash"]
                    await core.persist_session(live.record, live.config)
                    announcement = {"event": "subagent.message", "sessionId": handle.id, "data": {"task_id": "task-crash", "message_id": "m1", "parent_session_id": handle.id}}
                    runtime.sessions.route_daemon_event(announcement)
                    await _wait(lambda: not runtime.status()["sessions"]["pending"], label="the failed continuity turn")
                    lifecycle = [(e, d.get("origin")) for sid, e, d in listener.events if e in ("turn.started", "turn.completed", "turn.failed")]
                    self.assertEqual(lifecycle, [("turn.started", "user"), ("turn.completed", None), ("turn.started", "child_message"), ("turn.failed", None)])
                    self.assertEqual([e for e, _ in listener.runtime_events], ["session.continuity_failed"])
                    # Claimed before the provider ran: on disk as delivered, in the persisted transcript once.
                    self.assertEqual({m["status"] for m in await read_task_mailbox_messages("task-crash")}, {"delivered"})
                    stored = await core.load_session(handle.id)
                    self.assertEqual(json.dumps(stored["messages"]).count("child result before the crash"), 1)

                    provider.fail = False
                    runtime.sessions.route_daemon_event(announcement)   # a stale repeat after the crash: nothing to do
                    await _wait(lambda: not runtime.status()["sessions"]["pending"], label="skipped continuity")
                    self.assertEqual(len(provider.requests), 2)   # the user turn and the failed continuity turn
                    result = await handle.run_turn("what did the child say?", provider_override=provider.override)
                    self.assertEqual(result["stopReason"], "end_turn")
                    self.assertEqual(provider.transcripts()[-1].count("child result before the crash"), 1)   # once, not replayed
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_restart_with_a_queued_message_delivers_it(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[IDLE], os.environ[WATCH] = "300", "200"
            provider = Provider()

            async def scenario():
                first = await _start(home, workspace, Listener(), session_options=lambda sid, reason: SessionOptions(provider_override=provider.override))
                parent = await first.sessions.open()
                parent_id = parent.id
                await first.sessions.close(parent_id)
                await first.stop()
                # The child's result lands while no runtime is up.
                await _seed_task(workspace, parent_id, "task-restart", message="result written while the host was down")
                await _adopt_on_disk(parent_id, "task-restart")

                host_module._ACTIVE = None
                listener = Listener()
                second = await _start(home, workspace, listener, session_options=lambda sid, reason: SessionOptions(provider_override=provider.override))
                try:
                    await _wait(lambda: (parent_id, "child_message") in listener.opened, label="reopen after restart")
                    await _wait(lambda: not second.status()["sessions"]["pending"], label="the continuity turn")
                    self.assertEqual(len(provider.requests), 1)
                    self.assertIn("result written while the host was down", provider.transcripts()[0])
                    self.assertEqual({m["status"] for m in await read_task_mailbox_messages("task-restart")}, {"delivered"})
                finally:
                    await second.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_child_question_wakes_the_parent_and_an_unavailable_parent_gets_a_backstop_answer(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[IDLE], os.environ[WATCH], os.environ[REPLY] = "300", "200", "1000"
            provider = Provider("let me think")
            archived = set()

            def options_for(session_id, reason):
                if reason == "child_message" and session_id in archived:
                    return Unavailable("archived")
                return SessionOptions(provider_override=provider.override)

            async def _wait_answered(task_id):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if any(q.get("status") == "answered" for q in await read_task_questions(task_id)):
                        return
                    await asyncio.sleep(0.05)
                raise AssertionError("timeout waiting for the backstop answer")

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, session_options=options_for)
                try:
                    parent = await runtime.sessions.open()
                    await runtime.sessions.close(parent.id)
                    await _seed_task(workspace, parent.id, "task-question", status="running", question="Which branch should I use?")
                    await _adopt_on_disk(parent.id, "task-question")
                    await _wait(lambda: (parent.id, "child_message") in listener.opened, label="reopen for the child's question")
                    await _wait(lambda: not runtime.status()["sessions"]["pending"], label="the continuity turn")
                    transcript = provider.transcripts()[0]
                    self.assertIn("OPEN QUESTIONS FROM CHILD AGENTS", transcript)
                    self.assertIn("Which branch should I use?", transcript)
                    # The turn ended without answering: the engine's own backstop replied.
                    questions = await read_task_questions("task-question")
                    self.assertEqual((questions[-1]["status"], questions[-1]["answer"]), ("answered", "unanswered:parent_turn_exhausted"))

                    # A parent the host will not open: the runtime answers after the child's reply bound.
                    calls_before = len(provider.requests)   # the nudged turn above made several calls
                    other = await runtime.sessions.open()
                    await runtime.sessions.close(other.id)
                    archived.add(other.id)
                    await _seed_task(workspace, other.id, "task-orphan", status="running", question="Anyone there?")
                    await _adopt_on_disk(other.id, "task-orphan")
                    await _wait(lambda: any(p["state"] == "refused" for p in runtime.status()["sessions"]["pending"]), label="the refusal")
                    await _wait_answered("task-orphan")
                    questions = await read_task_questions("task-orphan")
                    self.assertEqual(questions[-1]["status"], "answered")
                    self.assertEqual(questions[-1]["answer"], "unanswered:parent_unavailable:archived")
                    # Answer persistence and its diagnostic event are separate
                    # asynchronous observations; wait for both before asserting.
                    await _wait(lambda: any(e == "subagent.question.unanswered" for e, _ in listener.runtime_events), label="the unanswered diagnostic event")
                    self.assertIn("subagent.question.unanswered", [e for e, _ in listener.runtime_events])
                    self.assertFalse(other.is_open)
                    self.assertEqual(len(provider.requests), calls_before)   # no turn ran for the archived parent
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)
