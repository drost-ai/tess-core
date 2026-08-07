from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path

from tests.support.agent_core_case import AgentCoreTestCase, core, run
from agent_core._utils import now_iso


hygiene_module = importlib.import_module("agent_core.platform_support.session_hygiene")

_REAL = "2026-06-17T22:13:26.545Z"
_BAD = "2026-07-11T16:08:40.392Z"
_OLD = "2026-06-15T22:34:53.564Z"
_DEAD_PID = 99_999_999


class Package29SessionHygieneTests(AgentCoreTestCase):
    def _record(self, workspace: Path, session_id: str, *, updated_at: str = _OLD) -> dict:
        record = core.createEmptySession(
            cwd=str(workspace), provider="openai", model="test-model", systemPrompt="test",
            permissionMode="default", sessionId=session_id, originalCwd=str(workspace),
        )
        record["createdAt"] = _OLD
        record["updatedAt"] = updated_at
        record["messages"] = [{"role": "user", "content": "hello"}]
        return record

    def _write_archive(self, session_id: str, persisted_at: str = _REAL) -> None:
        run(
            core.appendSessionTranscriptLogEntries(
                session_id,
                [{"type": "message", "session_id": session_id, "message_index": 0, "persisted_at": persisted_at, "role": "user", "content": "hello"}],
            )
        )

    def _write_stamped(self, record: dict, stamp: str, archive_at: str = _REAL) -> None:
        record["metadata"]["fullTranscriptLog"] = {
            "path": core.sessionTranscriptLogPath(record["id"]),
            "archivedMessageCount": 1,
            "lastArchivedAt": stamp,
        }
        run(core.saveSession(record))
        self._write_archive(record["id"], archive_at)

    def _snapshot_files(self, root: Path) -> dict[str, tuple[bytes, int]]:
        return {
            str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file()
        }

    def _mixed_fixture(self, home: Path, workspace: Path) -> dict[str, dict]:
        damaged = self._record(workspace, "71111111-1111-4111-8111-111111111111", updated_at=_BAD)
        self._write_stamped(damaged, _BAD)
        healthy = self._record(workspace, "72222222-2222-4222-8222-222222222222", updated_at=_REAL)
        self._write_stamped(healthy, _REAL)
        ambiguous = self._record(workspace, "73333333-3333-4333-8333-333333333333", updated_at="2026-07-12T00:00:00Z")
        self._write_stamped(ambiguous, _BAD)

        stale = self._record(workspace, "74444444-4444-4444-8444-444444444444")
        stale["metadata"]["taskIds"] = ["dead-running"]
        run(core.saveSession(stale))
        run(core.writeTaskMeta("dead-running", {"type": "background_command", "taskId": "dead-running", "sessionId": stale["id"], "status": "running", "pid": _DEAD_PID, "startedAt": "2026-01-01T00:00:00Z"}))

        live = self._record(workspace, "75555555-5555-4555-8555-555555555555")
        live["metadata"]["taskIds"] = ["live-running"]
        run(core.saveSession(live))
        run(core.writeTaskMeta("live-running", {"type": "background_command", "taskId": "live-running", "sessionId": live["id"], "status": "running", "pid": os.getpid(), "startedAt": _OLD}))

        foreign = self._record(workspace, "smoke-session-record")
        run(core.saveSession(foreign))
        sessions_dir = home / "sessions"
        (sessions_dir / "broken.json").write_text("{broken", encoding="utf-8")
        oversized = sessions_dir / "oversized.json"
        with oversized.open("wb") as handle:
            handle.truncate(core.MAX_SHALLOW_SESSION_BYTES + 1)
        (sessions_dir / "orphan.corrupt-123").write_text("backup", encoding="utf-8")
        (sessions_dir / "orphan.json.123.tmp").write_text("tmp", encoding="utf-8")
        (sessions_dir / "manual.lock").write_text("", encoding="utf-8")
        return {"damaged": damaged, "healthy": healthy, "ambiguous": ambiguous, "stale": stale, "live": live, "foreign": foreign}

    def test_mixed_report_is_bounded_read_only_and_classifies_live_authority(self):
        root, home, workspace, previous = self.temp_context()
        try:
            fixture = self._mixed_fixture(home, workspace)
            before = self._snapshot_files(home)
            report = run(core.buildSessionHygieneReport({"sampleLimit": 20}))
            after = self._snapshot_files(home)
            self.assertEqual(after, before)
            self.assertEqual(report["sessionCount"], 6)
            self.assertEqual(report["markerSessionCount"], 2)
            self.assertEqual(report["liveSessionCount"], 1)
            self.assertEqual(report["staleMarkerSessionCount"], 1)
            self.assertEqual(report["damagedTimestampCount"], 1)
            self.assertEqual(report["damagedTimestamps"][0]["sessionId"], fixture["damaged"]["id"])
            self.assertEqual(report["archiveTimestampDisagreementCount"], 2)
            self.assertEqual(report["unprovableArchiveTimestampCount"], 1)
            self.assertIn(fixture["stale"]["id"], report["staleMarkerSessionIds"])
            self.assertIn(fixture["live"]["id"], report["liveSessionIds"])
            self.assertIn("task:dead-running", report["deadRunningLikeRecordIds"])
            self.assertIn("smoke-session-record.json", report["foreignEntries"])
            self.assertTrue(any(item["name"] == "broken.json" for item in report["malformedRecords"]))
            self.assertTrue(any(item["name"] == "oversized.json" for item in report["oversizedRecords"]))
            self.assertIn("orphan.corrupt-123", report["corruptBackups"])
            self.assertIn("orphan.json.123.tmp", report["strayTempFiles"])
            self.assertIn("manual.lock", report["lockFiles"])
            self.assertLessEqual(len(report["markerSessionIds"]), report["sampleLimit"])

            doctor = run(core.collectDoctorReport(str(workspace), {"repoRoot": str(workspace)}))
            hygiene = next(check for check in doctor["checks"] if check["id"] == "session_hygiene")
            self.assertEqual(hygiene["status"], "warn")
            self.assertEqual(hygiene["details"]["damagedTimestampCount"], 1)
        finally:
            self.restore_home(root, previous)

    def test_dry_run_apply_exact_fields_and_second_apply_is_empty(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            damaged = self._record(workspace, "76666666-6666-4666-8666-666666666666", updated_at=_BAD)
            self._write_stamped(damaged, _BAD)
            target = Path(core.sessionPath(damaged["id"]))
            before_raw = target.read_bytes()
            before_mtime = target.stat().st_mtime_ns
            before = json.loads(before_raw)

            dry = run(core.repairSessionTimestamps())
            self.assertFalse(dry["applied"])
            self.assertEqual([item["sessionId"] for item in dry["planned"]], [damaged["id"]])
            self.assertEqual(target.read_bytes(), before_raw)
            self.assertEqual(target.stat().st_mtime_ns, before_mtime)

            applied = run(core.repairSessionTimestamps({"apply": True}))
            self.assertTrue(applied["applied"])
            self.assertEqual(len(applied["appliedRepairs"]), 1)
            self.assertNotEqual(applied["appliedRepairs"][0]["beforeHash"], applied["appliedRepairs"][0]["afterHash"])
            repaired = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(repaired["updatedAt"], _REAL)
            self.assertEqual(repaired["metadata"]["fullTranscriptLog"]["lastArchivedAt"], _REAL)

            def normalize(value):
                value = copy.deepcopy(value)
                value["updatedAt"] = None
                value["metadata"]["fullTranscriptLog"]["lastArchivedAt"] = None
                return value

            self.assertEqual(normalize(repaired), normalize(before))
            second = run(core.repairSessionTimestamps({"apply": True}))
            self.assertEqual(second["planned"], [])
            self.assertEqual(second["appliedRepairs"], [])
        finally:
            self.restore_home(root, previous)

    def test_apply_refuses_live_daemon_and_concurrent_change_invalidates_candidate(self):
        root, _home, workspace, previous = self.temp_context()
        original_sleep = hygiene_module.asyncio.sleep
        try:
            damaged = self._record(workspace, "77777777-7777-4777-8777-777777777777", updated_at=_BAD)
            self._write_stamped(damaged, _BAD)
            target = Path(core.sessionPath(damaged["id"]))
            run(core.saveDaemonState("hygiene-instance", {"pid": os.getpid(), "phase": "running", "startedAt": now_iso()}))
            refused = run(core.repairSessionTimestamps({"apply": True}))
            self.assertTrue(refused["refusedDaemonRunning"])
            self.assertFalse(refused["applied"])
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["updatedAt"], _BAD)
            self.assertEqual(len(run(core.repairSessionTimestamps())["planned"]), 1)
            run(core.clearDaemonState("hygiene-instance"))

            changed = False

            async def mutate_at_boundary(_delay):
                nonlocal changed
                if not changed:
                    changed = True
                    record = json.loads(target.read_text(encoding="utf-8"))
                    record["name"] = "concurrent semantic update"
                    target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

            hygiene_module.asyncio.sleep = mutate_at_boundary
            raced = run(core.repairSessionTimestamps({"apply": True}))
            self.assertEqual(raced["appliedRepairs"], [])
            self.assertEqual(raced["invalidated"], [{"sessionId": damaged["id"], "reason": "record_changed"}])
            after = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(after["updatedAt"], _BAD)
            self.assertEqual(after["name"], "concurrent semantic update")
        finally:
            hygiene_module.asyncio.sleep = original_sleep
            self.restore_home(root, previous)
