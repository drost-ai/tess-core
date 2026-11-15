from __future__ import annotations

import json
from unittest import mock

from tests.support.tess_core_case import *


class SessionDeltaFastPathTests(TessCoreTestCase):
    """Mid-batch tool persists must ride the head/delta commit, repair included."""

    def _session(self, workspace, session_id):
        session = core.create_empty_session(
            cwd=str(workspace),
            provider="openai",
            model="test-model",
            systemPrompt="test",
            permissionMode="default",
            sessionId=session_id,
            originalCwd=str(workspace),
        )
        session["messages"] = [{"role": "user", "content": "hello"}]
        return session

    def test_mid_batch_repair_persist_and_followup_stay_on_delta_path(self):
        import tess_core.session.persistence as persistence_module

        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "31111111-1111-4111-8111-111111111111")
            config = {"sessionPersistence": True, "cwd": str(workspace)}
            run(core.persist_session(session, config))

            session["messages"].append({"role": "assistant", "content": "working on it"})
            session["messages"].append(
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}
            )

            async def forbidden_full_transaction(*_args, **_kwargs):
                raise AssertionError("mid-batch persist fell back to the full transaction")

            with mock.patch.object(
                persistence_module, "mutate_session_record_transaction", side_effect=forbidden_full_transaction
            ):
                run(core.persist_session(session, config))
            persisted = run(core.load_session(session["id"]))
            # The dangling in-flight tool_use is repaired away on disk...
            self.assertEqual(len(persisted["messages"]), 2)
            # ...while the live session keeps its in-flight tail.
            self.assertEqual(len(session["messages"]), 3)

            session["messages"].append(
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}
            )
            with mock.patch.object(
                persistence_module, "mutate_session_record_transaction", side_effect=forbidden_full_transaction
            ):
                run(core.persist_session(session, config))
            final = run(core.load_session(session["id"]))
            self.assertEqual(len(final["messages"]), 4)
            self.assertEqual(final["messages"][2]["content"][0]["id"], "t1")
        finally:
            self.restore_home(root, previous)

    def test_truncated_copy_confines_lineage_to_the_unpersisted_tail(self):
        from tess_core.storage.message_history import SessionMessageList

        source = SessionMessageList(
            [{"role": "user", "content": f"m{index}"} for index in range(4)]
        )
        source.set_checkpoint(3, 2, "commit-a")

        tail_only = source.truncated_copy(3)
        self.assertEqual(len(tail_only), 3)
        self.assertEqual(tail_only.checkpoint(), source.checkpoint())

        into_prefix = source.truncated_copy(1)
        self.assertEqual(len(into_prefix), 1)
        self.assertIsNone(into_prefix.checkpoint())

        source.invalidate(0)
        self.assertIsNone(source.truncated_copy(3).checkpoint())

    def test_presanitized_commit_paths_still_write_sanitized_records(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "32222222-2222-4222-8222-222222222222")
            session["messages"].append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
                    ],
                }
            )
            config = {"sessionPersistence": True, "cwd": str(workspace)}
            run(core.persist_session(session, config))
            persisted = run(core.load_session(session["id"]))
            blocks = persisted["messages"][1]["content"]
            self.assertNotIn("QUJD", json.dumps(blocks))
            self.assertEqual(blocks[0], {"type": "text", "text": "look"})
        finally:
            self.restore_home(root, previous)

    def test_head_delta_transaction_contract_directly(self):
        from tess_core.storage.session_transactions import mutate_session_head_delta_transaction

        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "33333333-3333-4333-8333-333333333333")
            run(core.persist_session(session, {"sessionPersistence": True, "cwd": str(workspace)}))
            live = run(core.load_session(session["id"]))
            messages = live["messages"]
            base_revision = live["revision"]
            messages.append({"role": "assistant", "content": "delta suffix"})

            async def rename(head):
                return {"changed": True, "session": {**head, "name": "delta-named"}}

            committed = run(mutate_session_head_delta_transaction(session["id"], messages, rename))
            self.assertTrue(committed["fastPath"])
            self.assertEqual(committed["revision"], base_revision + 1)
            self.assertEqual(committed["session"]["name"], "delta-named")
            self.assertIs(committed["session"]["messages"], messages)
            self.assertEqual(messages.checkpoint().messageCount, 2)

            reloaded = run(core.load_session(session["id"]))
            self.assertEqual(reloaded["name"], "delta-named")
            self.assertEqual(len(reloaded["messages"]), 2)
            self.assertEqual(reloaded["messages"][-1]["content"], "delta suffix")

            # A plain list has no append authority; neither does a tracked
            # list whose checkpoint went stale against the store revision.
            async def untouched(head):
                return {"changed": False, "session": head}

            self.assertIsNone(
                run(mutate_session_head_delta_transaction(session["id"], list(messages), untouched))
            )
            stale = run(core.load_session(session["id"]))
            run(mutate_session_head_delta_transaction(session["id"], reloaded["messages"], rename))
            self.assertIsNone(
                run(mutate_session_head_delta_transaction(session["id"], stale["messages"], rename))
            )

            # A head mutation may not smuggle a message list past the body.
            async def with_messages(head):
                return {"changed": True, "session": {**head, "messages": []}}

            fresh = run(core.load_session(session["id"]))
            with self.assertRaises(TypeError):
                run(mutate_session_head_delta_transaction(session["id"], fresh["messages"], with_messages))
        finally:
            self.restore_home(root, previous)
