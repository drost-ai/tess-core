from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.tasks.communication import (
    create_task_question,
    read_task_events,
    read_task_mailbox_messages,
    send_task_message,
    task_mailbox_path,
)
from agent_core.tasks.mailbox_closure import (
    TaskMailboxClosedError,
    close_mailbox_if_idle,
    read_mailbox_closure,
)
from agent_core.tasks.runners.resident_idle import ResidentIdleClock, expire_if_idle
from tests import test_resident_subagent as resident_support  # module import: its test classes must not be collected twice


async def _seed_task(workspace, task_id="idle-task", *, status="awaiting_input"):
    await core.write_task_meta(task_id, {
        "type": "subagent", "taskId": task_id, "id": task_id, "agent_id": "idle-agent",
        "sessionId": "idle-child", "session_id": "idle-child",
        "parent_session_id": "idle-parent", "status": status, "waitingReason": "resident_idle",
        "resultSummary": "the last result", "cwd": str(workspace),
        "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
    })
    await core.save_task_record({
        "id": task_id, "sessionId": "idle-child", "sourceId": task_id, "kind": "subagent",
        "status": status, "title": "idle worker", "summary": "the last result",
        "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "owner": "daemon",
    })
    await core.save_child_agent({
        "agentId": "idle-agent", "sessionId": "idle-child", "parentSessionId": "idle-parent",
        "taskId": task_id, "name": "Idle", "status": status, "waitingReason": "resident_idle",
        "resultSummary": "the last result", "runInBackground": True, "stoppable": True,
        "startedAt": "2026-01-01T00:00:00Z",
    })


class MailboxClosureTests(AgentCoreTestCase):
    def test_empty_mailbox_closes_and_a_later_message_fails_clearly(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await _seed_task(workspace)
                path = task_mailbox_path("idle-task")
                self.assertTrue(await close_mailbox_if_idle(path, "idle-task", "idle-child", "idle_expired"))
                self.assertTrue(await close_mailbox_if_idle(path, "idle-task", "idle-child", "idle_expired"))  # idempotent
                with self.assertRaises(TaskMailboxClosedError) as raised:
                    await send_task_message("idle-task", {"direction": "to_child", "content": "one more thing"})
                self.assertEqual(raised.exception.reason, "idle_expired")
                self.assertIn("no longer accepts messages", str(raised.exception))
                # Nothing was queued behind the closure, and the control record is not a message.
                self.assertEqual(await read_task_mailbox_messages("idle-task"), [])
                self.assertEqual((await read_mailbox_closure(path))["reason"], "idle_expired")
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_a_queued_message_keeps_the_mailbox_open(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await _seed_task(workspace)
                path = task_mailbox_path("idle-task")
                await send_task_message("idle-task", {"direction": "to_child", "content": "arrived in time"})
                self.assertFalse(await close_mailbox_if_idle(path, "idle-task", "idle-child", "idle_expired"))
                self.assertIsNone(await read_mailbox_closure(path))
                # The child's own results to its parent do not keep it alive.
                await core.mark_task_message_delivered("idle-task", (await read_task_mailbox_messages("idle-task"))[0]["messageId"])
                await send_task_message("idle-task", {"direction": "to_parent", "content": "a result"})
                self.assertTrue(await close_mailbox_if_idle(path, "idle-task", "idle-child", "idle_expired"))
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_racing_senders_are_delivered_or_refused_never_lost(self):
        """The invariant behind the idle limit: a message either lands before
        the closure (and would have kept the child alive) or its sender is
        told no. None may sit behind the closure unread."""
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await _seed_task(workspace)
                path = task_mailbox_path("idle-task")
                outcomes = {}

                async def send(index):
                    await asyncio.sleep(0.001 * (index % 5))
                    try:
                        await send_task_message("idle-task", {"direction": "to_child", "content": f"message {index}", "messageId": f"m-{index}"})
                        outcomes[index] = "sent"
                    except TaskMailboxClosedError:
                        outcomes[index] = "refused"

                async def close_when_possible():
                    while not await close_mailbox_if_idle(path, "idle-task", "idle-child", "idle_expired"):
                        for message in await read_task_mailbox_messages("idle-task"):
                            if message.get("status") == "queued":
                                await core.mark_task_message_delivered("idle-task", message["messageId"])

                await asyncio.gather(close_when_possible(), *(send(index) for index in range(24)))
                from agent_core.storage.json_io import read_json_lines

                records = await read_json_lines(path)
                closed_at = next(i for i, record in enumerate(records) if record.get("kind") == "mailbox_closed")
                first_seen = {}
                for position, record in enumerate(records):
                    first_seen.setdefault(record.get("messageId"), position)
                for index, outcome in outcomes.items():
                    if outcome == "sent":
                        self.assertLess(first_seen[f"m-{index}"], closed_at)
                    else:
                        self.assertNotIn(f"m-{index}", first_seen)
                self.assertEqual(len(outcomes), 24)
            run(exercise())
        finally:
            self.restore_home(root, previous)


class ResidentIdleDecisionTests(AgentCoreTestCase):
    def test_clock_counts_idle_time_only(self):
        clock = ResidentIdleClock(50)
        self.assertFalse(clock.due())
        time.sleep(0.07)
        self.assertTrue(clock.due())
        clock.restart()  # a turn just published its result
        self.assertFalse(clock.due())
        time.sleep(0.07)
        clock.still_busy()  # something of its own is still open: idleness counts from now
        self.assertFalse(clock.due())

    def test_truly_idle_child_is_stopped_for_good_with_its_reason(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await _seed_task(workspace)
                self.assertTrue(await expire_if_idle("idle-task", "idle-agent", "idle-child", ResidentIdleClock(900000)))
                child = await core.load_child_agent("idle-agent")
                self.assertEqual((child["status"], child["stopReason"], child["completionReason"]), ("stopped", "idle_expired", "idle_expired"))
                self.assertFalse(child["stoppable"])
                self.assertIsNotNone(child["completedAt"])
                self.assertEqual(child["resultSummary"], "the last result")  # what it produced is untouched
                meta = await core.read_task_meta("idle-task")
                self.assertEqual((meta["status"], meta["detailStatus"], meta["stopReason"]), ("stopped", "idle_expired", "idle_expired"))
                self.assertEqual(meta["summary"], "the last result")
                events = [event["type"] for event in await read_task_events("idle-task")]
                self.assertIn("task.subagent.idle_expired", events)
                with self.assertRaises(TaskMailboxClosedError):
                    await send_task_message("idle-task", {"direction": "to_child", "content": "too late"})
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_open_question_own_task_or_descendant_keeps_the_child(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await _seed_task(workspace)
                clock = ResidentIdleClock(900000)

                question = await create_task_question("idle-task", {"question": "which one?", "options": ["a", "b"]})
                self.assertFalse(await expire_if_idle("idle-task", "idle-agent", "idle-child", clock))
                await core.skip_task_question("idle-task", question["questionId"], "test")

                await core.save_task_record({
                    "id": "own-shell", "sessionId": "idle-child", "sourceId": "own-shell", "kind": "background_command",
                    "status": "running", "title": "its own build", "summary": "running",
                    "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "owner": "daemon",
                })
                self.assertFalse(await expire_if_idle("idle-task", "idle-agent", "idle-child", clock))
                await core.save_task_record({
                    "id": "own-shell", "sessionId": "idle-child", "sourceId": "own-shell", "kind": "background_command",
                    "status": "completed", "title": "its own build", "summary": "done",
                    "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:01Z", "owner": "daemon",
                })

                await core.save_child_agent({
                    "agentId": "grandchild", "sessionId": "grandchild-session", "parentSessionId": "idle-child",
                    "status": "running", "runInBackground": True, "startedAt": "2026-01-01T00:00:00Z",
                })
                self.assertFalse(await expire_if_idle("idle-task", "idle-agent", "idle-child", clock))
                await core.save_child_agent({
                    "agentId": "grandchild", "sessionId": "grandchild-session", "parentSessionId": "idle-child",
                    "status": "completed", "runInBackground": True, "startedAt": "2026-01-01T00:00:00Z",
                })

                self.assertIsNone(await read_mailbox_closure(task_mailbox_path("idle-task")))
                self.assertEqual((await core.load_child_agent("idle-agent"))["status"], "awaiting_input")
                self.assertTrue(await expire_if_idle("idle-task", "idle-agent", "idle-child", clock))
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_a_message_that_arrived_in_time_restarts_the_clock(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await _seed_task(workspace)
                clock = ResidentIdleClock(30)
                await asyncio.sleep(0.05)
                self.assertTrue(clock.due())
                await send_task_message("idle-task", {"direction": "to_child", "content": "just in time"})
                self.assertFalse(await expire_if_idle("idle-task", "idle-agent", "idle-child", clock))
                self.assertFalse(clock.due())
                self.assertEqual((await core.load_child_agent("idle-agent"))["status"], "awaiting_input")
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_first_terminal_state_wins(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await _seed_task(workspace)
                child = await core.load_child_agent("idle-agent")
                await core.save_child_agent({**child, "status": "cancelled", "completionReason": "cancelled"})
                meta = await core.read_task_meta("idle-task")
                await core.write_task_meta("idle-task", {**meta, "status": "terminated", "detailStatus": "stopped_by_request"})
                await expire_if_idle("idle-task", "idle-agent", "idle-child", ResidentIdleClock(900000))
                self.assertEqual((await core.load_child_agent("idle-agent"))["status"], "cancelled")
                self.assertEqual((await core.read_task_meta("idle-task"))["detailStatus"], "stopped_by_request")
            run(exercise())
        finally:
            self.restore_home(root, previous)


class SendMessageToFinishedChildTests(AgentCoreTestCase):
    def test_parent_is_told_clearly_and_nothing_is_queued(self):
        from agent_core.tools.messaging import _send_message_tool

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await _seed_task(workspace)
                await expire_if_idle("idle-task", "idle-agent", "idle-child", ResidentIdleClock(900000))
                handler = _send_message_tool().handler
                context = {"session": {"id": "idle-parent", "metadata": {}}, "config": {}, "cwd": str(workspace)}
                result = await handler({"to": "idle-agent", "message": "are you still there?"}, context)
                self.assertTrue(result["isError"])
                detail = json.loads(result["content"])["detail"]
                self.assertIn("idle_expired", detail)
                self.assertIn("start a new sub-agent", detail)
                self.assertEqual(await read_task_mailbox_messages("idle-task"), [])
            run(exercise())
        finally:
            self.restore_home(root, previous)


class ResidentIdleProcessTests(AgentCoreTestCase):
    """Real worker processes: process exit is the claim being tested."""

    def _start(self, home, workspace, events):
        seeder = resident_support.ResidentSubagentTests()
        _config, parent, child = seeder._seed_family(home, workspace)
        supervisor = run(core.create_task_supervisor({"cwd": str(workspace), "onEvent": events.append}))
        started = run(supervisor["startSubagentTask"]({
            "parentSessionId": parent["id"], "sessionId": child["id"],
            "agentId": "resident-agent", "cwd": str(workspace), "label": "Resident",
        }))
        return seeder, supervisor, parent, child, started["taskId"]

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def test_idle_child_exits_by_itself_and_is_recorded_as_stopped(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            os.environ["AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS"] = "100"
            os.environ["AGENT_CORE_SUBAGENTS_RESIDENT_IDLE_TTL_MS"] = "1200"
            events = []
            seeder, supervisor, parent, child, task_id = self._start(home, workspace, events)
            idle_meta = seeder._wait_for(
                lambda: (lambda m: m if m and m.get("status") == "awaiting_input" else None)(run(core.read_task_meta(task_id))),
                label="resident idle after its first turn",
            )
            pid = idle_meta.get("pid") or (run(core.load_task_record(task_id)) or {}).get("pid")
            seeder._wait_for(
                lambda: (run(core.load_task_record(task_id)) or {}).get("status") == "stopped",
                label="task record projected as stopped after the idle limit",
            )
            meta = run(core.read_task_meta(task_id))
            self.assertEqual((meta["detailStatus"], meta["stopReason"]), ("idle_expired", "idle_expired"))
            child_record = run(core.load_child_agent("resident-agent"))
            self.assertEqual((child_record["status"], child_record["stopReason"]), ("stopped", "idle_expired"))
            if isinstance(pid, int):
                seeder._wait_for(lambda: not self._alive(pid), label="worker process exit")
            # Its result was delivered before it went; nothing is re-announced as new.
            results = [m for m in run(read_task_mailbox_messages(task_id)) if m.get("kind") == "result"]
            self.assertEqual(len(results), 1)
            # The supervisor announced the stop to the parent session.
            stopped = [e for e in events if e.get("event") == "task.stopped" and (e.get("data") or {}).get("task_id") == task_id]
            self.assertEqual(len(stopped), 1)
            # The transcript is still there, and a late message is refused.
            self.assertTrue(run(core.load_session(child["id"]))["messages"])
            with self.assertRaises(TaskMailboxClosedError):
                run(send_task_message(task_id, {"direction": "to_child", "content": "too late"}))
        finally:
            os.environ.pop("AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS", None)
            os.environ.pop("AGENT_CORE_SUBAGENTS_RESIDENT_IDLE_TTL_MS", None)
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_message_before_the_limit_is_handled_and_restarts_the_clock(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            os.environ["AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS"] = "100"
            os.environ["AGENT_CORE_SUBAGENTS_RESIDENT_IDLE_TTL_MS"] = "2500"
            events = []
            seeder, supervisor, parent, child, task_id = self._start(home, workspace, events)
            seeder._wait_for(
                lambda: (run(core.read_task_meta(task_id)) or {}).get("status") == "awaiting_input",
                label="resident idle after its first turn",
            )
            time.sleep(1.2)  # well inside the limit
            run(send_task_message(task_id, {"direction": "to_child", "content": "follow up please"}))
            seeder._wait_for(
                lambda: len([m for m in run(read_task_mailbox_messages(task_id)) if m.get("kind") == "result"]) >= 2,
                label="second turn result",
            )
            answered_at = time.monotonic()
            seeder._wait_for(
                lambda: (run(core.load_task_record(task_id)) or {}).get("status") == "stopped",
                label="stopped after a fresh idle period",
            )
            # The clock restarted with the second result: it did not stop straight away.
            self.assertGreater(time.monotonic() - answered_at, 1.5)
        finally:
            os.environ.pop("AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS", None)
            os.environ.pop("AGENT_CORE_SUBAGENTS_RESIDENT_IDLE_TTL_MS", None)
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)
