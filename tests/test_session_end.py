"""``end``: the one explicit instruction. Real daemon and real child worker
processes (their provider profile has no key, so a child fails its first
turn fast and then waits, resident, for its idle limit): process exit is the
claim. Every test is one coroutine.
"""
from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core import SessionEnded, SessionOptions, start_runtime
from tess_core._utils import is_process_alive
from tess_core.config._knobs import reset_configured_knobs
from tess_core.runtime import host as host_module
from tess_core.storage import load_daemon_state, load_monitor, save_monitor
from tess_core.storage.records import save_task_record
from tess_core.tasks.communication import create_task_question, read_task_mailbox_messages, read_task_questions, send_task_message

_ENV = ("TESS_CORE_SUBAGENTS_RESIDENT_POLL_MS", "TESS_CORE_TEST_MISSING_END_KEY", "TESS_CORE_SESSIONS_IDLE_CLOSE_MS")
_LIVE = {"starting", "queued", "running", "backgrounded", "awaiting_input"}


class Listener:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.runtime_events: list[tuple[str, dict]] = []
        self.events: list[tuple[str, str, dict]] = []

    def on_event(self, session_id, envelope):
        self.events.append((session_id, envelope["event"], envelope["data"]))

    def on_session_opened(self, session_id, reason):
        self.opened.append((session_id, reason))

    def on_session_closed(self, session_id, reason):
        self.closed.append((session_id, reason))

    def on_runtime_event(self, event, data):
        self.runtime_events.append((event, data))


def _reply(text="ok"):
    async def send(_request):
        return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {}, "content": [{"type": "text", "text": text}]}

    return {"send": send}


async def _wait(predicate, timeout=30.0, label=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for {label}")


async def _wait_async(predicate, timeout=30.0, label=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for {label}")


def _settings(home):
    path = home / "settings.json"
    path.write_text(json.dumps({
        "provider": "end-openai",
        "providerProfiles": {"end-openai": {"kind": "openai", "defaultModel": "end-model", "auth": {"kind": "openai_api_key", "envVar": "TESS_CORE_TEST_MISSING_END_KEY"}}},
    }), encoding="utf-8")
    return path


async def _spawn_child(runtime, parent_session_id, agent_id, settings_path, workspace, config):
    """A real resident child of that session, started through the daemon."""
    child = core.create_empty_session(
        cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"],
        effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions",
    )
    child["messages"].append({"role": "user", "content": "report in"})
    child["metadata"].update({"parentSessionId": parent_session_id, "agentId": agent_id, "tessCoreSettingsPath": str(settings_path)})
    child["metadata"]["effectiveAgentAuthority"] = await core.capture_agent_authority(config=config, session=child, tools=core.create_builtin_tools())
    await core.save_session(child)
    await core.save_child_agent({
        "agentId": agent_id, "sessionId": child["id"], "parentSessionId": parent_session_id, "name": agent_id, "provider": config["provider"],
        "model": config["model"], "mode": "bypassPermissions", "runInBackground": True, "status": "backgrounded",
        "startedAt": "2026-01-01T00:00:00Z", "effectiveAgentAuthority": child["metadata"]["effectiveAgentAuthority"],
    })
    started = await runtime.daemon["startSubagentTask"]({"parentSessionId": parent_session_id, "sessionId": child["id"], "agentId": agent_id, "cwd": str(workspace), "label": agent_id})
    return child["id"], started["taskId"]


async def _child_pid(agent_id):
    record = await core.load_child_agent(agent_id)
    pid = (record or {}).get("pid")
    return pid if isinstance(pid, int) and record.get("status") in _LIVE else None


class SessionEndTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None
        os.environ["TESS_CORE_SUBAGENTS_RESIDENT_POLL_MS"] = "100"

    def tearDown(self):
        for name in _ENV:
            os.environ.pop(name, None)
        reset_configured_knobs()

    def test_end_stops_the_sessions_children_and_descendants_and_nothing_else(self):
        root, home, workspace, previous = self.temp_context()
        try:
            settings_path = _settings(home)

            def options_for(session_id, reason):
                return SessionOptions(provider="end-openai", settings=str(settings_path), permission_mode="bypassPermissions", provider_override=_reply())

            async def scenario():
                listener = Listener()
                runtime = await start_runtime(str(home), cwd=str(workspace), listener=listener, session_options=options_for, startup_timeout_seconds=20)
                try:
                    alpha = await runtime.sessions.open()
                    bravo = await runtime.sessions.open()
                    for handle in (alpha, bravo):
                        await handle.run_turn("start children")
                    config = runtime.sessions.live(alpha.id).config
                    child_a, task_a = await _spawn_child(runtime, alpha.id, "child-a", settings_path, workspace, config)
                    child_b, task_b = await _spawn_child(runtime, bravo.id, "child-b", settings_path, workspace, config)
                    pid_a = await _wait_async(lambda: _child_pid("child-a"), label="child a alive")
                    pid_b = await _wait_async(lambda: _child_pid("child-b"), label="child b alive")
                    grand, task_g = await _spawn_child(runtime, child_a, "grand-a", settings_path, workspace, config)
                    pid_g = await _wait_async(lambda: _child_pid("grand-a"), label="grandchild alive")
                    self.assertTrue(all(is_process_alive(pid) for pid in (pid_a, pid_b, pid_g)))
                    daemon_pid = (await load_daemon_state(runtime.instance_id))["pid"]

                    outcome = await runtime.sessions.end(alpha.id, reason="deleted")
                    self.assertFalse(outcome["alreadyEnded"])
                    self.assertEqual(set(outcome["stopped"]["stoppedTasks"]), {task_a, task_g})
                    self.assertEqual(outcome["stopped"]["failures"], [])
                    self.assertIn(child_a, outcome["stopped"]["sessions"])
                    await _wait(lambda: not is_process_alive(pid_a) and not is_process_alive(pid_g), label="a's child and grandchild gone")
                    self.assertTrue(is_process_alive(pid_b))          # another session's child: untouched
                    self.assertTrue(is_process_alive(daemon_pid))     # the daemon: untouched
                    for agent_id in ("child-a", "grand-a"):
                        record = await core.load_child_agent(agent_id)
                        self.assertEqual((record["status"], record["stopReason"]), ("stopped", "session_ended"))
                    for task_id in (task_a, task_g):
                        meta = await core.read_task_meta(task_id)
                        self.assertEqual(meta["stopReason"], "session_ended")
                    self.assertEqual((await core.load_child_agent("child-b"))["status"], "backgrounded")

                    self.assertIn((alpha.id, "ended"), listener.closed)
                    self.assertFalse(alpha.is_open)
                    self.assertTrue(bravo.is_open)
                    stored = await core.load_session(alpha.id)
                    self.assertEqual(stored["metadata"]["ended"]["reason"], "deleted")
                    self.assertTrue(stored["metadata"]["ended"]["at"])
                    with self.assertRaises(SessionEnded):
                        await alpha.run_turn("anyone?")
                    with self.assertRaises(SessionEnded):
                        await runtime.sessions.open(alpha.id)
                    again = await runtime.sessions.end(alpha.id)
                    self.assertTrue(again["alreadyEnded"])
                    self.assertEqual(again["stopped"]["stoppedTasks"], [])  # nothing left to stop
                    self.assertEqual((await bravo.run_turn("still here?"))["stopReason"], "end_turn")
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_end_fails_what_was_still_addressed_to_the_session(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False,
                                              session_options=lambda sid, reason: SessionOptions(provider_override=_reply()), startup_timeout_seconds=20)
                try:
                    handle = await runtime.sessions.open()
                    await handle.run_turn("spawn something")
                    live = runtime.sessions.live(handle.id)
                    task_id = "task-addressed"
                    await core.write_task_meta(task_id, {"type": "subagent", "taskId": task_id, "id": task_id, "sessionId": "child-x", "session_id": "child-x",
                                                         "parent_session_id": handle.id, "status": "running", "cwd": str(workspace),
                                                         "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"})
                    await save_task_record({"id": task_id, "sessionId": handle.id, "kind": "subagent", "status": "running", "title": task_id,
                                            "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"})
                    await send_task_message(task_id, {"direction": "to_parent", "content": "result nobody will read", "kind": "result", "parentSessionId": handle.id})
                    await create_task_question(task_id, {"childSessionId": "child-x", "question": "Which branch?", "options": ["main"]})
                    live.record["metadata"]["taskIds"] = [task_id]
                    await core.persist_session(live.record, live.config)
                    monitor = core.create_monitor_record({"sessionId": handle.id, "taskId": task_id, "goal": "watch"})
                    await save_monitor(monitor)
                    from tess_core.subagents.reconciliation import list_unconsumed_subagent_reconciliations, read_subagent_reconciliations, upsert_subagent_reconciliation
                    await upsert_subagent_reconciliation({
                        "parentSessionId": handle.id, "taskId": "task-finished-earlier", "childAgentId": "child-y", "label": "child-y",
                        "terminalStatus": "failed", "terminalReason": "worker_failed", "summary": "a result the parent never took",
                    })

                    outcome = await runtime.sessions.end(handle.id)
                    self.assertEqual((outcome["failedMessages"], outcome["answeredQuestions"], outcome["stoppedMonitors"], outcome["settledReconciliations"]), (1, 1, 1, 1))
                    # Nothing left to wake a parent that can never take it; the record says why it was settled.
                    self.assertEqual(await list_unconsumed_subagent_reconciliations(handle.id), [])
                    (settled,) = await read_subagent_reconciliations(handle.id)
                    self.assertEqual((settled["parentTurnId"], settled.get("parentMessageIndex")), ("ended:deleted", None))
                    messages = await read_task_mailbox_messages(task_id)
                    self.assertEqual([(m["status"], m.get("failureReason")) for m in messages if m["direction"] == "to_parent"], [("failed", "session_ended")])
                    # The backstop answer itself is a to_child message, queued for a child that is being stopped.
                    self.assertEqual([m["status"] for m in messages if m["direction"] == "to_child"], ["queued"])
                    questions = await read_task_questions(task_id)
                    self.assertEqual((questions[-1]["status"], questions[-1]["answer"]), ("answered", "unanswered:session_ended"))
                    self.assertTrue((await load_monitor(monitor["id"]))["deliveryStoppedAt"])
                    self.assertIsNone(outcome["stopped"])  # no daemon: nothing to stop through it
                    self.assertEqual(listener.closed, [(handle.id, "ended")])
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_terminal_childs_reconciliation_for_a_parent_that_cannot_take_it_is_settled_at_once(self):
        """The daemon writes a reconciliation entry when a child ends. An ended
        parent can never consume it, so the entry is settled as it is
        written, with the reason, and never wakes anyone; a live parent takes
        it in its next turn."""
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                from tess_core.subagents.reconciliation import (
                    create_subagent_reconciliation_for_terminal_child, list_unconsumed_subagent_reconciliations, read_subagent_reconciliations,
                )

                def meta(parent, task):
                    return {"parent_session_id": parent, "task_id": task, "session_id": "child-" + task, "agent_id": "agent-" + task,
                            "status": "stopped", "completionReason": "cancelled", "title": "orphan " + task, "updatedAt": "2026-01-01T00:00:00Z"}

                runtime = await start_runtime(str(home), cwd=str(workspace), daemon_enabled=False,
                                              session_options=lambda sid, reason: SessionOptions(provider_override=_reply()), startup_timeout_seconds=20)
                try:
                    live = await runtime.sessions.open()
                    await live.run_turn("hi")
                    ended = await runtime.sessions.open()
                    await ended.run_turn("hi")
                    await runtime.sessions.end(ended.id, reason="deleted")
                    await create_subagent_reconciliation_for_terminal_child(taskMeta=meta(ended.id, "t2"))
                    await create_subagent_reconciliation_for_terminal_child(taskMeta=meta(live.id, "t3"))
                    self.assertEqual(await list_unconsumed_subagent_reconciliations(ended.id), [])
                    self.assertEqual((await read_subagent_reconciliations(ended.id))[0]["parentTurnId"], "ended:deleted")
                    self.assertEqual(len(await list_unconsumed_subagent_reconciliations(live.id)), 1)  # a live parent takes it in its next turn
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_end_during_a_running_turn_cancels_it_and_a_restart_still_refuses_the_session(self):
        root, home, workspace, previous = self.temp_context()
        try:
            asked = []

            def options_for(session_id, reason):
                asked.append((session_id, reason))
                return SessionOptions(provider_override=_reply())

            async def scenario():
                listener = Listener()
                runtime = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False, session_options=options_for, startup_timeout_seconds=20)
                gate = asyncio.Event()

                async def blocked(_request):
                    await gate.wait()
                    return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "late"}]}

                try:
                    handle = await runtime.sessions.open()
                    running = asyncio.ensure_future(handle.run_turn("take your time", turn_id="t-slow", provider_override={"send": blocked}))
                    await _wait(lambda: handle.turns()["running"], label="turn running")
                    outcome = await runtime.sessions.end(handle.id, reason="deleted")
                    self.assertEqual((await running)["stopReason"], "cancelled")   # end is the owner's intent: the turn is cancelled
                    self.assertEqual(listener.closed, [(handle.id, "ended")])
                    self.assertFalse(handle.is_open)
                    self.assertFalse(outcome["alreadyEnded"])
                    with self.assertRaises(SessionEnded):
                        await handle.run_turn("again?")
                    session_id = handle.id
                finally:
                    gate.set()
                    await runtime.stop()

                host_module._ACTIVE = None
                asked.clear()
                listener = Listener()
                restarted = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False, session_options=options_for, startup_timeout_seconds=20)
                try:
                    with self.assertRaises(SessionEnded) as refused:
                        await restarted.sessions.open(session_id)
                    self.assertEqual((refused.exception.reason, bool(refused.exception.at)), ("deleted", True))
                    self.assertTrue(restarted.sessions.is_ended(session_id))
                    # A child's message for it: refused for good, the host never asked.
                    restarted.sessions.route_daemon_event({"event": "subagent.message", "sessionId": session_id, "data": {"task_id": "t", "message_id": "m", "parent_session_id": session_id}})
                    await asyncio.sleep(0.1)
                    self.assertEqual(asked, [])
                    self.assertEqual(restarted.status()["sessions"]["pending"], [])
                    self.assertEqual(listener.opened, [])
                finally:
                    await restarted.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)
