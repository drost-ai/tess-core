from __future__ import annotations

import json
import time
import tracemalloc
from pathlib import Path

from tests.support.agent_core_case import AgentCoreTestCase, core, run


class Package29StoreScaleTests(AgentCoreTestCase):
    def test_two_thousand_session_store_cache_and_hygiene_ceilings(self):
        root, home, workspace, previous = self.temp_context()
        try:
            sessions_dir = home / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            count = 2_000
            archive_payload_bytes = 0
            for index in range(count):
                session_id = f"80000000-0000-4000-8000-{index:012x}"
                stamp = f"2026-06-01T00:{index % 60:02d}:{index % 60:02d}Z"
                record = {
                    "id": session_id,
                    "cwd": str(workspace),
                    "createdAt": "2026-06-01T00:00:00Z",
                    "updatedAt": stamp,
                    "provider": "openai",
                    "model": "test-model",
                    "systemPrompt": "test",
                    "permissionMode": "default",
                    "messages": [{"role": "user", "content": f"message {index}"}],
                    "todos": [],
                    "metadata": {
                        "originalCwd": str(workspace),
                        "fullTranscriptLog": {
                            "path": str(sessions_dir / session_id / "full-transcript-with-tools.jsonl"),
                            "archivedMessageCount": 1,
                            "lastArchivedAt": stamp,
                        },
                    },
                }
                (sessions_dir / f"{session_id}.json").write_text(json.dumps(record), encoding="utf-8")
                archive_dir = sessions_dir / session_id
                archive_dir.mkdir()
                padding = "x" * ((index % 7) * 64)
                line = json.dumps({"type": "message", "message_index": 0, "persisted_at": stamp, "role": "user", "content": f"message {index}{padding}"}) + "\n"
                (archive_dir / "full-transcript-with-tools.jsonl").write_text(line, encoding="utf-8")
                archive_payload_bytes += len(line.encode())
            (sessions_dir / "foreign-note.txt").write_text("preserve me", encoding="utf-8")

            cache = core.create_session_list_cache()
            started = time.perf_counter()
            self.assertEqual(len(run(cache["list"]())), count)
            first_elapsed = time.perf_counter() - started
            first = cache["getStats"]()
            self.assertEqual(first["parsed"], count)
            self.assertLess(first_elapsed, 15)

            started = time.perf_counter()
            self.assertEqual(len(run(cache["list"]())), count)
            second_elapsed = time.perf_counter() - started
            second = cache["getStats"]()
            self.assertEqual(second["parsed"], count)
            self.assertEqual(second["cacheHits"] - first["cacheHits"], count)
            self.assertLess(second_elapsed, 10)

            changed_id = "80000000-0000-4000-8000-000000000123"
            changed_path = Path(core.session_path(changed_id))
            changed = json.loads(changed_path.read_text(encoding="utf-8"))
            changed["name"] = "changed once"
            changed_path.write_text(json.dumps(changed), encoding="utf-8")
            run(cache["list"]())
            third = cache["getStats"]()
            self.assertEqual(third["parsed"] - second["parsed"], 1)

            tracemalloc.start()
            started = time.perf_counter()
            report = run(core.buildSessionHygieneReport())
            report_elapsed = time.perf_counter() - started
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(report["sessionCount"], count)
            self.assertEqual(report["archiveReads"], count)
            self.assertEqual(report["damagedTimestampCount"], 0)
            self.assertEqual(report["archiveBytesRead"], archive_payload_bytes)
            self.assertLess(report_elapsed, 20)
            self.assertLess(peak, 128 * 1024 * 1024)
            self.assertTrue((sessions_dir / "foreign-note.txt").exists())
        finally:
            self.restore_home(root, previous)
