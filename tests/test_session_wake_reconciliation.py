"""A child's reconciliation, not only its mailbox, reaches its parent: a failed
one-shot child wakes its closed parent, and work for a parent this home has no
record of is refused by the runtime without asking the host."""
from __future__ import annotations

from tests.support.tess_core_case import *
from tests.support.session_wake import *
from tests.support.session_wake import _ENV, _adopt_on_disk, _seed_task, _start, _wait

from tess_core import SessionOptions
from tess_core.config._knobs import reset_configured_knobs
from tess_core.runtime import host as host_module


class SessionWakeReconciliationTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def tearDown(self):
        for name in _ENV:
            os.environ.pop(name, None)
        reset_configured_knobs()

    def test_work_for_a_parent_with_no_record_is_refused_without_asking_the_host(self):
        """Task files copied from another runtime home name parents that never
        existed here. Their reconciliations are announced like any other, but
        the runtime refuses the reopen itself: the host is not asked about a
        session it could not know, and the refusal shows in status."""
        root, home, workspace, previous = self.temp_context()
        try:
            asked: list[str] = []

            def options_for(session_id, reason):
                asked.append(session_id)
                return SessionOptions(provider_override=Provider().override)

            async def scenario():
                from tess_core.subagents.reconciliation import upsert_subagent_reconciliation

                listener = Listener()
                runtime = await _start(home, workspace, listener, session_options=options_for)
                try:
                    await _seed_task(workspace, "ghost-parent", "task-ghost", status="failed")
                    await upsert_subagent_reconciliation({
                        "parentSessionId": "ghost-parent", "taskId": "task-ghost", "childAgentId": "child-ghost", "label": "ghost",
                        "terminalStatus": "failed", "terminalReason": "cancelled", "summary": "a result for a parent that never existed here",
                    })
                    await _wait(lambda: any(p["sessionId"] == "ghost-parent" and p["state"] == "refused" for p in runtime.status()["sessions"]["pending"]), label="the refusal")
                    (pending,) = [p for p in runtime.status()["sessions"]["pending"] if p["sessionId"] == "ghost-parent"]
                    self.assertEqual((pending["reason"], pending["retry"]), ("unknown_session", False))
                    self.assertNotIn("ghost-parent", asked)
                    self.assertIn(("session.reopen_refused", "unknown_session"), [(e, d.get("reason")) for e, d in listener.runtime_events])
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_failed_child_wakes_its_closed_parent_through_its_reconciliation(self):
        """A one-shot child that fails leaves a reconciliation entry, not a
        mailbox message (seen in the real-provider canary: a Codex 400 in the
        child left the parent closed forever). It is announced like one."""
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[IDLE], os.environ[WATCH] = "300", "200"
            provider = Provider("noted the failure")

            async def scenario():
                from tess_core.subagents.reconciliation import list_unconsumed_subagent_reconciliations, upsert_subagent_reconciliation

                listener = Listener()
                runtime, = (await _start(home, workspace, listener, session_options=lambda sid, reason: SessionOptions(provider_override=provider.override)),)
                try:
                    parent = await runtime.sessions.open()
                    await parent.run_turn("start a child, please")
                    await runtime.sessions.close(parent.id)
                    await _seed_task(workspace, parent.id, "task-failed-child", status="failed")
                    await _adopt_on_disk(parent.id, "task-failed-child")
                    await upsert_subagent_reconciliation({
                        "parentSessionId": parent.id, "taskId": "task-failed-child", "childAgentId": "child-x", "label": "child-x",
                        "terminalStatus": "failed", "terminalReason": "provider rejected the request (400)",
                        "summary": "the child failed before reporting anything",
                    })
                    await _wait(lambda: (parent.id, "child_message") in listener.opened, label="reopen for the child's failure")
                    await _wait(lambda: not runtime.status()["sessions"]["pending"], label="the continuity turn")
                    self.assertEqual(len(provider.requests), 2)
                    self.assertIn("the child failed before reporting anything", provider.transcripts()[1])
                    self.assertIn("Background subagent updates", provider.transcripts()[1])
                    self.assertEqual(await list_unconsumed_subagent_reconciliations(parent.id), [])   # consumed by the turn
                    await asyncio.sleep(1.0)   # no re-announcement once consumed
                    self.assertEqual(len(provider.requests), 2)
                    self.assertEqual(listener.opened.count((parent.id, "child_message")), 1)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)
