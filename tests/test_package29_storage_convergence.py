from __future__ import annotations

import asyncio
import builtins
import importlib
import json
import os
import time
from pathlib import Path

from tests.support.agent_core_case import AgentCoreTestCase, core, run


sessions_module = importlib.import_module("agent_core.storage.sessions")
projection_module = importlib.import_module("agent_core.tasks.supervisor_parts.projection")


class Package29StorageConvergenceTests(AgentCoreTestCase):
    def _session(self, workspace: Path, session_id: str) -> dict:
        session = core.createEmptySession(
            cwd=str(workspace),
            provider="openai",
            model="test-model",
            systemPrompt="test",
            permissionMode="default",
            sessionId=session_id,
            originalCwd=str(workspace),
        )
        session["createdAt"] = "2026-06-01T00:00:00Z"
        session["updatedAt"] = "2026-06-01T00:00:00Z"
        session["messages"] = [{"role": "user", "content": "hello"}]
        return session

    def test_touch_policy_and_zero_append_preserve_semantic_timestamps(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "11111111-1111-4111-8111-111111111111")
            run(core.saveSession(session))
            config = {"sessionPersistence": True, "cwd": str(workspace)}

            run(core.persistSession(session, config, touch=False))
            first = run(core.loadSessionRecordShallow(session["id"]))
            first_log = dict(first["metadata"]["fullTranscriptLog"])
            archive = Path(core.sessionTranscriptLogPath(session["id"]))
            first_archive = archive.read_bytes()
            self.assertEqual(first["updatedAt"], "2026-06-01T00:00:00Z")
            self.assertTrue(first_log["lastArchivedAt"])

            run(core.persistSession(session, config, touch=False))
            second = run(core.loadSessionRecordShallow(session["id"]))
            self.assertEqual(second["updatedAt"], first["updatedAt"])
            self.assertEqual(second["metadata"]["fullTranscriptLog"]["lastArchivedAt"], first_log["lastArchivedAt"])
            self.assertEqual(archive.read_bytes(), first_archive)

            run(core.persistSession(session, config))
            semantic = run(core.loadSessionRecordShallow(session["id"]))
            self.assertGreater(semantic["updatedAt"], first["updatedAt"])
            self.assertEqual(archive.read_bytes(), first_archive)
        finally:
            self.restore_home(root, previous)

    def test_listing_is_shallow_and_selected_load_repairs_once(self):
        root, _home, workspace, previous = self.temp_context()
        original_open = builtins.open
        try:
            session = self._session(workspace, "22222222-2222-4222-8222-222222222222")
            session["metadata"]["fullTranscriptLog"] = {
                "path": core.sessionTranscriptLogPath(session["id"]),
                "archivedMessageCount": 99,
                "lastArchivedAt": "2026-06-01T00:00:00Z",
            }
            run(core.saveSession(session))
            run(
                core.appendSessionTranscriptLogEntries(
                    session["id"],
                    [
                        {"type": "message", "message_index": 0, "role": "user", "content": "hello", "persisted_at": "2026-06-01T00:00:01Z"},
                        {"type": "message", "message_index": 1, "role": "assistant", "content": "world", "persisted_at": "2026-06-01T00:00:02Z"},
                    ],
                )
            )
            target = Path(core.sessionPath(session["id"]))
            before = target.read_bytes()
            before_mtime = target.stat().st_mtime_ns
            archive_opens = []

            def tracking_open(path, *args, **kwargs):
                if str(path).endswith("full-transcript-with-tools.jsonl"):
                    archive_opens.append(str(path))
                return original_open(path, *args, **kwargs)

            sessions_module.open = tracking_open
            listed = run(core.listSessions())
            self.assertEqual([item["id"] for item in listed], [session["id"]])
            self.assertEqual(archive_opens, [])
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(target.stat().st_mtime_ns, before_mtime)

            first = run(core.loadSessionWithReconciliation(session["id"]))
            self.assertTrue(first["changed"])
            self.assertEqual(len(first["record"]["messages"]), 2)
            self.assertEqual(first["record"]["metadata"]["fullTranscriptLog"]["archivedMessageCount"], 2)
            after_first = target.read_bytes()
            after_first_mtime = target.stat().st_mtime_ns
            time.sleep(0.01)
            second = run(core.loadSessionWithReconciliation(session["id"]))
            self.assertFalse(second["changed"])
            self.assertEqual(target.read_bytes(), after_first)
            self.assertEqual(target.stat().st_mtime_ns, after_first_mtime)
        finally:
            if hasattr(sessions_module, "open"):
                delattr(sessions_module, "open")
            self.restore_home(root, previous)

    def test_summary_cache_reparses_only_changed_records(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            sessions = [
                self._session(workspace, f"33333333-3333-4333-8333-{index:012x}")
                for index in range(3)
            ]
            for session in sessions:
                run(core.saveSession(session))
            cache = core.createSessionListCache()
            self.assertEqual(len(run(cache["list"]())), 3)
            first = cache["getStats"]()
            self.assertEqual(first["parsed"], 3)
            self.assertEqual(len(run(cache["list"]())), 3)
            second = cache["getStats"]()
            self.assertEqual(second["parsed"], 3)
            self.assertEqual(second["cacheHits"] - first["cacheHits"], 3)

            changed = sessions[1]
            changed["name"] = "changed"
            run(core.saveSession(changed))
            run(cache["list"]())
            third = cache["getStats"]()
            self.assertEqual(third["parsed"] - second["parsed"], 1)
            self.assertEqual(third["cacheHits"] - second["cacheHits"], 2)
        finally:
            self.restore_home(root, previous)

    def test_projection_metadata_converges_without_touching_recency(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "44444444-4444-4444-8444-444444444444")
            run(core.saveSession(session))
            run(projection_module._project_session_task_records(session["id"], session))
            target = Path(core.sessionPath(session["id"]))
            archive = Path(core.sessionTranscriptLogPath(session["id"]))
            first_raw = target.read_bytes()
            first_archive = archive.read_bytes()
            first_mtime = target.stat().st_mtime_ns
            first = json.loads(first_raw)
            self.assertEqual(first["updatedAt"], "2026-06-01T00:00:00Z")

            time.sleep(0.01)
            run(projection_module._project_session_task_records(session["id"], session))
            self.assertEqual(target.read_bytes(), first_raw)
            self.assertEqual(archive.read_bytes(), first_archive)
            self.assertEqual(target.stat().st_mtime_ns, first_mtime)
        finally:
            self.restore_home(root, previous)

    def test_simulated_ten_minute_idle_gate_has_zero_session_or_archive_writes(self):
        root, _home, workspace, previous = self.temp_context()
        runtime = None
        try:
            session = self._session(workspace, "55555555-5555-4555-8555-555555555555")
            run(core.saveSession(session))
            run(projection_module._project_session_task_records(session["id"], session))
            runtime = run(
                core.createCollaborationRuntime(
                    {"session": session, "config": {"sessionPersistence": True}, "startTicker": False}
                )
            )
            session_file = Path(core.sessionPath(session["id"]))
            archive_file = Path(core.sessionTranscriptLogPath(session["id"]))
            before = (session_file.read_bytes(), session_file.stat().st_mtime_ns, archive_file.read_bytes(), archive_file.stat().st_mtime_ns)

            # 400 source-equivalent 1.5 s observations = ten simulated idle minutes.
            for _ in range(400):
                run(projection_module._project_session_task_records(session["id"], session))
                run(runtime["refresh"]())
            after = (session_file.read_bytes(), session_file.stat().st_mtime_ns, archive_file.read_bytes(), archive_file.stat().st_mtime_ns)
            self.assertEqual(after, before)
        finally:
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)
