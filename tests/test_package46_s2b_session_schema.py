from __future__ import annotations

import importlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import agent_core._facade as core
from agent_core.storage.persistence_metrics import register_persistence_observer
from tests.support.agent_core_case import AgentCoreTestCase, run


_CRASH_SCRIPT = r"""
import asyncio, os, signal, sys
import agent_core._facade as core

async def main():
    home, session_id, boundary, mode = sys.argv[1:]
    with core.scoped_runtime_home(home):
        def inject(actual):
            if actual == boundary:
                os.kill(os.getpid(), signal.SIGKILL)
        if mode == "migrate":
            await core.migrate_session_to_v2(session_id, faultInjector=inject)
            return
        async def mutate(latest):
            latest["messages"].append({"role": "assistant", "content": boundary})
            return {"session": latest, "changed": True}
        await core.mutate_session_record_transaction(
            session_id, mutate, faultInjector=inject
        )

asyncio.run(main())
"""


class Package46S2bSchemaTests(AgentCoreTestCase):
    def _session(self, workspace: Path, session_id: str, count: int = 1) -> dict:
        session = core.create_empty_session(
            cwd=str(workspace),
            provider="anthropic",
            model="claude-sonnet-4-6",
            systemPrompt="Package 46 S2b",
            permissionMode="default",
            sessionId=session_id,
        )
        session["messages"] = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"message-{index}"}
            for index in range(count)
        ]
        return session

    def test_first_ordinary_persist_migrates_v1_without_special_caller_logic(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "package46-first-persist", 2)
            run(core.write_json_file(core.session_path(session["id"]), session))
            run(
                core.persistSession(session, {"sessionPersistence": True}, touch=False)
            )
            head = run(core.read_json_file(core.session_path(session["id"]), None))
            self.assertEqual(head["schemaVersion"], 2)
            self.assertEqual(head["messageCount"], 2)
            self.assertEqual(head["revision"], 1)
            restored = run(core.load_session(session["id"]))
            self.assertEqual(list(restored["messages"]), list(session["messages"]))
            self.assertEqual(restored["revision"], 1)
        finally:
            self.restore_home(root, previous)

    def test_v1_migration_and_cli_writeback_are_byte_exact(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "package46-s2b-migration", 3)
            run(core.write_json_file(core.session_path(session["id"]), session))
            transcript = Path(core.session_transcript_log_path(session["id"]))
            run(core.append_session_transcript_log_entries(session["id"], [{"type": "sentinel"}]))
            original_head = Path(core.session_path(session["id"])).read_bytes()
            original_transcript = transcript.read_bytes()

            report = run(core.migrate_session_to_v2(session["id"]))
            self.assertTrue(report["changed"])
            head = run(core.read_json_file(core.session_path(session["id"]), None))
            self.assertEqual(head["schemaVersion"], 2)
            self.assertEqual(head["messageCount"], 3)
            self.assertNotIn("messages", head)
            self.assertEqual(
                run(core.hydrate_persisted_session(session["id"], head))["record"],
                session,
            )
            self.assertEqual(transcript.read_bytes(), original_transcript)
            with self.assertRaises(core.SessionSchemaVersionError) as unsupported:
                core.assert_session_schema_supported(head, 1)
            self.assertEqual(unsupported.exception.code, "session.schema_unsupported")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_core",
                    "session-write-back-v1",
                    session["id"],
                    "--runtime-home",
                    str(home),
                ],
                cwd=Path(__file__).parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["schemaVersion"], 1)
            self.assertEqual(Path(core.session_path(session["id"])).read_bytes(), original_head)
            self.assertEqual(transcript.read_bytes(), original_transcript)
        finally:
            self.restore_home(root, previous)

    def test_sigkill_at_each_commit_boundary_recovers_only_committed_head(self):
        root, home, workspace, previous = self.temp_context()
        try:
            for boundary, expected_count in (
                ("before_body", 1),
                ("after_body", 1),
                ("after_head", 2),
            ):
                with self.subTest(boundary=boundary):
                    session = self._session(workspace, f"package46-crash-{boundary}")
                    run(core.save_session(session))
                    completed = self._crash(home, session["id"], boundary, "commit")
                    self.assertEqual(completed.returncode, -signal.SIGKILL)
                    restored = run(core.load_session(session["id"]))
                    self.assertEqual(len(restored["messages"]), expected_count)
                    self.assertEqual(
                        run(core.load_session(session["id"]))["messages"],
                        restored["messages"],
                    )
        finally:
            self.restore_home(root, previous)

    def test_killed_v1_migration_is_retryable_and_preserves_v1_authority(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "package46-crash-migration", 2)
            run(core.write_json_file(core.session_path(session["id"]), session))
            completed = self._crash(home, session["id"], "after_body", "migrate")
            self.assertEqual(completed.returncode, -signal.SIGKILL)
            raw = run(core.read_json_file(core.session_path(session["id"]), None))
            self.assertNotIn("schemaVersion", raw)
            self.assertEqual(raw, session)
            self.assertEqual(run(core.load_session(session["id"])), session)
            run(core.migrate_session_to_v2(session["id"]))
            self.assertEqual(run(core.load_session(session["id"])), session)
        finally:
            self.restore_home(root, previous)

    def test_short_body_repairs_head_and_torn_tail_is_removed_on_next_append(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "package46-body-repair", 3)
            run(core.save_session(session))
            body = Path(core.session_messages_path(session["id"]))
            lines = body.read_bytes().splitlines(keepends=True)
            body.write_bytes(b"".join(lines[:3]))
            verdict = run(core.load_session_with_reconciliation(session["id"]))
            self.assertTrue(verdict["changed"])
            self.assertEqual(verdict["reason"], "body_shorter")
            self.assertEqual(len(verdict["record"]["messages"]), 2)
            head = run(core.read_json_file(core.session_path(session["id"]), None))
            self.assertEqual(head["messageCount"], 2)

            with body.open("ab") as handle:
                handle.write(b'{"type":"message","session_id":')

            async def append(latest):
                latest["messages"].append({"role": "user", "content": "after-torn-tail"})
                return {"session": latest, "changed": True}

            run(core.mutate_session_record_transaction(session["id"], append))
            restored = run(core.load_session(session["id"]))
            self.assertEqual([item["content"] for item in restored["messages"]], [
                "message-0", "message-1", "after-torn-tail"
            ])
            self.assertTrue(all(json.loads(line) for line in body.read_text().splitlines()))
        finally:
            self.restore_home(root, previous)

    def test_truncate_marker_compaction_list_and_fork_resume_are_exact(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            parent = self._session(workspace, "package46-parent", 4)
            run(core.save_session(parent))

            async def shrink(latest):
                latest["messages"] = latest["messages"][:2]
                return {"session": latest, "changed": True}

            run(core.mutate_session_record_transaction(parent["id"], shrink))
            body = Path(core.session_messages_path(parent["id"]))
            self.assertIn('"type": "truncate"', body.read_text())
            compacted = run(core.compact_session_message_body(parent["id"]))
            self.assertEqual(compacted["messageCount"], 2)
            self.assertNotIn('"type": "truncate"', body.read_text())
            restored_parent = run(core.load_session(parent["id"]))
            self.assertEqual(restored_parent["messages"], parent["messages"][:2])
            self.assertIn(parent["id"], [item["id"] for item in run(core.list_sessions())])
            shallow = run(core.load_session_record_shallow(parent["id"]))
            self.assertNotIn("messages", shallow)
            self.assertTrue(shallow["hasPersistableContent"])

            child = core.createForkedSession(
                parent=restored_parent,
                id="package46-child",
                cwd=str(workspace),
                originalCwd=str(workspace),
                now="2026-08-01T00:00:00Z",
                model=parent["model"],
                systemPrompt=parent["systemPrompt"],
                permissionMode=parent["permissionMode"],
            )
            run(core.save_session(child))
            self.assertEqual(run(core.load_session(child["id"]))["messages"], restored_parent["messages"])
        finally:
            self.restore_home(root, previous)

    def test_append_watermark_commits_without_hydrating_the_persisted_prefix(self):
        root, _home, workspace, previous = self.temp_context()
        schema = importlib.import_module("agent_core.storage.session_schema_v2")
        original_read = schema._read_body_sync
        try:
            session = self._session(workspace, "package46-fast-watermark", 3)
            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            session = run(core.load_session(session["id"]))
            events: list[dict] = []
            session["messages"].append({"role": "assistant", "content": "delta"})

            def forbidden_read(*_args):
                raise AssertionError("fast append hydrated the persisted body")

            schema._read_body_sync = forbidden_read
            unsubscribe = register_persistence_observer(events.append)
            try:
                run(
                    core.persistSession(
                        session, {"sessionPersistence": True}, touch=False
                    )
                )
            finally:
                unsubscribe()
            io = [event for event in events if event.get("event") == "storage_io"]
            self.assertEqual([event["operation"] for event in io], ["append", "append", "write"])
            schema._read_body_sync = original_read
            reloaded = run(core.load_session(session["id"]))
            self.assertEqual(reloaded["messages"][-1]["content"], "delta")
        finally:
            schema._read_body_sync = original_read
            self.restore_home(root, previous)

    def test_delta_commit_snapshots_the_live_list_before_awaits(self):
        root, _home, workspace, previous = self.temp_context()
        schema = importlib.import_module("agent_core.storage.session_schema_v2")
        original_read = schema._read_body_sync
        try:
            session = self._session(workspace, "package46-delta-race", 1)
            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            session = run(core.load_session(session["id"]))
            session["messages"].append({"role": "assistant", "content": "committed"})
            raced = {"role": "user", "content": "raced append"}

            def inject(boundary):
                if boundary == "after_body":
                    session["messages"].append(raced)

            def forbidden_read(*_args):
                raise AssertionError("fast append hydrated the persisted body")

            schema._read_body_sync = forbidden_read
            try:
                run(
                    core.persistSession(
                        session,
                        {"sessionPersistence": True},
                        touch=False,
                        faultInjector=inject,
                    )
                )
            finally:
                schema._read_body_sync = original_read

            head = run(core.read_json_file(core.session_path(session["id"]), None))
            commits = [
                entry
                for entry in map(
                    json.loads,
                    Path(core.session_messages_path(session["id"])).read_text().splitlines(),
                )
                if entry["type"] == "commit"
            ]
            self.assertEqual(head["messageCount"], 2)
            self.assertEqual(commits[-1]["message_count"], 2)
            restored = run(core.load_session_with_reconciliation(session["id"]))
            self.assertEqual(
                [message["content"] for message in restored["record"]["messages"]],
                ["message-0", "committed"],
            )

            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            final = run(core.load_session(session["id"]))
            self.assertEqual(len(final["messages"]), 3)
            self.assertEqual(final["messages"][-1], raced)
        finally:
            schema._read_body_sync = original_read
            self.restore_home(root, previous)

    def test_nested_prefix_mutation_falls_back_and_persists_the_edit(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "package46-prefix-edit", 2)
            session["messages"][0]["content"] = [
                {"type": "text", "text": "original"}
            ]
            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            session["messages"][0]["content"][0]["text"] = "edited"
            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            restored = run(core.load_session(session["id"]))
            self.assertEqual(
                restored["messages"][0]["content"][0]["text"], "edited"
            )
            body_types = [
                json.loads(line)["type"]
                for line in Path(core.session_messages_path(session["id"]))
                .read_text()
                .splitlines()
            ]
            self.assertIn("truncate", body_types[1:])
        finally:
            self.restore_home(root, previous)

    def test_repaired_history_rebinds_nested_mutation_authority(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "package46-repair-rebind", 0)
            session["messages"] = [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "original"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "missing-result",
                            "name": "Read",
                            "input": {},
                        }
                    ],
                },
            ]
            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            repair = core.repairSessionTranscript(session)
            self.assertTrue(repair["repaired"])
            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))

            session["messages"][0]["content"][0]["text"] = "edited after repair"
            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            restored = run(core.load_session(session["id"]))
            self.assertEqual(
                restored["messages"][0]["content"][0]["text"],
                "edited after repair",
            )
        finally:
            self.restore_home(root, previous)

    def test_commit_marker_hides_same_length_after_body_crash(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "package46-same-length-crash", 2)
            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            original = run(core.load_session(session["id"]))

            async def replace_prefix(latest):
                latest["messages"][0]["content"] = "uncommitted replacement"
                return {"session": latest, "changed": True}

            def inject(boundary):
                if boundary == "after_body":
                    raise RuntimeError("injected after-body crash")

            with self.assertRaisesRegex(RuntimeError, "after-body"):
                run(
                    core.mutate_session_record_transaction(
                        session["id"], replace_prefix, faultInjector=inject
                    )
                )
            self.assertEqual(run(core.load_session(session["id"])), original)

            session["messages"].append({"role": "assistant", "content": "retry"})
            run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            restored = run(core.load_session(session["id"]))
            self.assertEqual(restored["messages"][-1]["content"], "retry")
            self.assertNotIn("uncommitted replacement", json.dumps(restored))
        finally:
            self.restore_home(root, previous)

    @staticmethod
    def _crash(home: Path, session_id: str, boundary: str, mode: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", _CRASH_SCRIPT, str(home), session_id, boundary, mode],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
