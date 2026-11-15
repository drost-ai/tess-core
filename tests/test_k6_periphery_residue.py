from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core.storage.json_io import StorageTransactionError, with_record_transaction
from tess_core.tools.shell import _normalize_bash_timeout


class K6PeripheryResidueTests(TessCoreTestCase):
    def test_torn_daemon_events_tail_keeps_the_replay_stream(self):
        root, home, _workspace, previous = self.temp_context()
        try:
            instance = "torn-tail-instance"
            run(core.append_daemon_event(instance, {"event": "one"}))
            run(core.append_daemon_event(instance, {"event": "two"}))
            path = Path(core.daemon_events_path(instance))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write('{"event": "torn')
            events = run(core.load_daemon_events(instance))
            self.assertEqual([event["event"] for event in events], ["one", "two"])
            self.assertEqual(run(core.read_last_daemon_event_cursor(instance)), 2)
        finally:
            self.restore_home(root, previous)

    def test_bash_timeout_zero_and_negative_mean_the_default(self):
        self.assertEqual(_normalize_bash_timeout("echo hi", 0), 120000)
        self.assertEqual(_normalize_bash_timeout("echo hi", -5), 120000)
        self.assertEqual(_normalize_bash_timeout("echo hi", 30), 30000)
        self.assertEqual(_normalize_bash_timeout("echo hi", None), 120000)

    def test_provider_failure_persist_repairs_the_dangling_tail(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace), provider="openai", model="test-model",
                systemPrompt="test", permissionMode="default",
            )
            session["messages"] = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            ]
            config = {"sessionPersistence": True, "cwd": str(workspace)}
            run(core.persist_provider_failure(session, config, {"category": "timeout"}))
            persisted = run(core.load_session(session["id"]))
            # The dangling in-flight tool_use is repaired away, exactly like
            # an ordinary persist; it used to slip through and wobble the
            # archive counts on the next save.
            self.assertEqual(len(persisted["messages"]), 1)
            self.assertEqual(len(session["messages"]), 2)
        finally:
            self.restore_home(root, previous)

    def test_spawned_task_does_not_inherit_lock_reentrancy(self):
        root, _home, _workspace, previous = self.temp_context()
        try:
            async def scenario():
                outcomes = {}

                async def child():
                    try:
                        async with with_record_transaction("k6-lock", "key", timeoutMs=200):
                            outcomes["child"] = "entered"
                    except StorageTransactionError:
                        outcomes["child"] = "blocked"

                async with with_record_transaction("k6-lock", "key"):
                    # Same-task nesting stays reentrant.
                    async with with_record_transaction("k6-lock", "key"):
                        outcomes["nested"] = "entered"
                    # A spawned task inherits the context copy but must not
                    # treat the parent's holdings as its own.
                    await asyncio.create_task(child())
                return outcomes

            outcomes = run(scenario())
            self.assertEqual(outcomes["nested"], "entered")
            self.assertEqual(outcomes["child"], "blocked")
        finally:
            self.restore_home(root, previous)
