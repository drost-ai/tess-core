from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import agent_core as core
from tests.support.agent_core_case import AgentCoreTestCase, run

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MutateJsonFileTests(AgentCoreTestCase):
    def test_mutate_creates_from_fallback_and_returns_updated(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "value.json")
            result = run(core.mutate_json_file(path, lambda value: {**value, "a": 1}, {}))
            self.assertEqual(result, {"a": 1})
            self.assertEqual(json.loads(Path(path).read_text()), {"a": 1})
        finally:
            self.restore_home(root, previous)

    def test_in_place_mutator_returning_none_persists_received_value(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "value.json")
            run(core.write_json_file(path, {"items": []}))

            def add(value):
                value["items"].append("x")

            result = run(core.mutate_json_file(path, add, {}))
            self.assertEqual(result, {"items": ["x"]})
            self.assertEqual(json.loads(Path(path).read_text()), {"items": ["x"]})
        finally:
            self.restore_home(root, previous)

    def test_locked_write_bytes_identical_to_write_json_file(self):
        root, home, workspace, previous = self.temp_context()
        try:
            value = {"b": [1, 2], "a": {"nested": None}, "text": "ünïcode"}
            plain = str(home / "plain.json")
            locked = str(home / "locked.json")
            run(core.write_json_file(plain, value))
            run(core.mutate_json_file(locked, lambda _: value, {}))
            self.assertEqual(Path(plain).read_bytes(), Path(locked).read_bytes())
        finally:
            self.restore_home(root, previous)

    def test_concurrent_coroutine_mutations_all_survive(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "shared.json")

            async def writer(index: int) -> None:
                await core.mutate_json_file(path, lambda value: {**value, f"key{index}": index}, {})

            async def main() -> None:
                await asyncio.gather(*(writer(index) for index in range(16)))

            run(main())
            record = json.loads(Path(path).read_text())
            self.assertEqual(sorted(record), [f"key{i}" for i in sorted(range(16), key=str)])
        finally:
            self.restore_home(root, previous)

    def test_concurrent_process_mutations_all_survive(self):
        """The cross-process regression: bare load->mutate->save loses writes."""
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "shared.json")
            script = textwrap.dedent(
                """
                import asyncio, sys
                sys.path.insert(0, sys.argv[3])
                import agent_core as core

                async def main():
                    index = sys.argv[2]
                    for round in range(5):
                        await core.mutate_json_file(
                            sys.argv[1],
                            lambda value: {**value, f"proc{index}_round{round}": int(index)},
                            {},
                        )

                asyncio.run(main())
                """
            )
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", script, path, str(index), REPO_ROOT],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for index in range(4)
            ]
            for proc in procs:
                _, stderr = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0, stderr.decode("utf-8", "replace"))
            record = json.loads(Path(path).read_text())
            expected = {f"proc{p}_round{r}" for p in range(4) for r in range(5)}
            self.assertEqual(set(record), expected)
        finally:
            self.restore_home(root, previous)

    def test_lock_timeout_notifies_observer_and_fails_closed(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "contended.json")
            events: list[dict] = []
            unsubscribe = core.register_storage_lock_observer(events.append)
            try:

                async def real_main() -> None:
                    release = asyncio.Event()
                    entered = asyncio.Event()

                    async def holder() -> None:
                        async with core.file_lock(path):
                            entered.set()
                            await release.wait()

                    holder_task = asyncio.create_task(holder())
                    await entered.wait()
                    try:
                        with self.assertRaises(core.StorageTransactionError) as error:
                            async with core.file_lock(path, timeoutMs=150):
                                pass
                        self.assertEqual(error.exception.code, "storage.lock_timeout")
                    finally:
                        release.set()
                        await holder_task

                run(real_main())
            finally:
                unsubscribe()
            self.assertTrue(events, "expected a lock-timeout notification")
            self.assertEqual(events[0]["action"], "failed_closed")
            self.assertEqual(events[0]["layer"], "in_process")
        finally:
            self.restore_home(root, previous)

    def test_cross_process_lock_blocks_second_writer_until_release(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "ordered.json")
            holder_script = textwrap.dedent(
                """
                import asyncio, sys, time
                sys.path.insert(0, sys.argv[2])
                import agent_core as core

                async def main():
                    async with core.file_lock(sys.argv[1]):
                        print("locked", flush=True)
                        await asyncio.sleep(1.0)
                        await core.write_json_file(sys.argv[1], {"writer": "holder"})

                asyncio.run(main())
                """
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_script, path, REPO_ROOT],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(holder.stdout.readline().strip(), "locked")

            async def contender() -> None:
                await core.mutate_json_file(path, lambda value: {**value, "writer2": True}, {})

            run(contender())
            holder.communicate(timeout=30)
            record = json.loads(Path(path).read_text())
            # The contender waited for the holder's lock, so its merge ran
            # after the holder's write and both writers are visible.
            self.assertEqual(record.get("writer"), "holder")
            self.assertTrue(record.get("writer2"))
        finally:
            self.restore_home(root, previous)

    def test_crash_between_temp_write_and_replace_leaves_no_partial_file(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "crash.json")
            run(core.write_json_file(path, {"state": "old"}))
            crasher = textwrap.dedent(
                """
                import asyncio, os, sys
                sys.path.insert(0, sys.argv[2])
                import agent_core as core
                from agent_core.storage import json_io

                real_replace = os.replace

                def exploding_replace(src, dst):
                    os._exit(9)

                async def main():
                    os.replace = exploding_replace
                    await core.mutate_json_file(sys.argv[1], lambda value: {"state": "new"}, {})

                asyncio.run(main())
                """
            )
            proc = subprocess.run(
                [sys.executable, "-c", crasher, path, REPO_ROOT],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 9)
            self.assertEqual(json.loads(Path(path).read_text()), {"state": "old"})
            run(core.mutate_json_file(path, lambda value: {**value, "after": True}, {}))
            self.assertEqual(json.loads(Path(path).read_text()), {"state": "old", "after": True})
        finally:
            self.restore_home(root, previous)

    def test_lock_timeout_event_name_is_known(self):
        from agent_core.runtime.events.shared import KNOWN_RUNTIME_EVENTS

        self.assertIn("storage.lock_timeout", KNOWN_RUNTIME_EVENTS)


class RecordMutationHelperTests(AgentCoreTestCase):
    def test_concurrent_child_agent_mutations_preserve_both_writers(self):
        """Finalizer-vs-reconciler shape: merge updates from two coroutines survive."""
        root, home, workspace, previous = self.temp_context()
        try:
            base = {
                "agentId": "agent-x",
                "sessionId": "child-1",
                "parentSessionId": "parent-1",
                "name": "x",
                "description": "x",
                "status": "running",
                "startedAt": "2026-06-10T00:00:00.000Z",
            }
            run(core.save_child_agent(base))

            async def finalize() -> None:
                await core.mutate_child_agent(
                    "agent-x", lambda current: {**current, "status": "completed", "resultSummary": "done"}
                )

            async def reconcile() -> None:
                await core.mutate_child_agent(
                    "agent-x", lambda current: {**current, "recoveryState": "recovered_after_daemon_restart"}
                )

            async def main() -> None:
                await asyncio.gather(finalize(), reconcile())

            run(main())
            record = run(core.load_child_agent("agent-x"))
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["resultSummary"], "done")
            self.assertEqual(record["recoveryState"], "recovered_after_daemon_restart")
        finally:
            self.restore_home(root, previous)

    def test_mutate_child_agent_skips_when_missing_and_mutator_declines(self):
        root, home, workspace, previous = self.temp_context()
        try:
            result = run(core.mutate_child_agent("agent-missing", lambda current: None))
            self.assertIsNone(result)
            self.assertIsNone(run(core.load_child_agent("agent-missing")))
        finally:
            self.restore_home(root, previous)

    def test_mutate_child_agent_creates_when_mutator_supplies_record(self):
        root, home, workspace, previous = self.temp_context()
        try:
            created = run(
                core.mutate_child_agent(
                    "agent-new",
                    lambda current: current
                    or {
                        "agentId": "agent-new",
                        "sessionId": "s",
                        "parentSessionId": "p",
                        "name": "n",
                        "description": "d",
                        "status": "failed",
                        "startedAt": "2026-06-10T00:00:00.000Z",
                        "error": None,
                    },
                )
            )
            self.assertEqual(created["status"], "failed")
            record = run(core.load_child_agent("agent-new"))
            # None-valued fields are omitted exactly like save_child_agent.
            self.assertNotIn("error", record)
        finally:
            self.restore_home(root, previous)

    def test_mutate_session_record_merges_metadata_under_lock(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-sonnet-4-6",
                systemPrompt="prompt",
                permissionMode="default",
            )
            run(core.save_session(session))

            async def add_task(task_id: str) -> None:
                def mutator(record):
                    metadata = record.setdefault("metadata", {})
                    metadata["taskIds"] = [*(metadata.get("taskIds") or []), task_id]
                    return record

                await core.mutate_session_record(session["id"], mutator)

            async def main() -> None:
                await asyncio.gather(*(add_task(f"task-{index}") for index in range(8)))

            run(main())
            loaded = run(core.load_session(session["id"]))
            self.assertEqual(
                sorted(loaded["metadata"]["taskIds"]), sorted(f"task-{index}" for index in range(8))
            )
        finally:
            self.restore_home(root, previous)

    def test_mutate_session_record_returns_none_for_missing_session(self):
        root, home, workspace, previous = self.temp_context()
        try:
            self.assertIsNone(run(core.mutate_session_record("missing-session", lambda record: record)))
        finally:
            self.restore_home(root, previous)

    def test_concurrent_task_meta_mutations_preserve_both_writers(self):
        """Exit-watcher-vs-stop shape: field updates from both writers survive."""
        root, home, workspace, previous = self.temp_context()
        try:
            base = {
                "type": "background_command",
                "taskId": "task-x",
                "id": "task-x",
                "status": "running",
                "startedAt": "2026-06-10T00:00:00.000Z",
                "outputFile": "/tmp/out.log",
            }
            run(core.write_task_meta("task-x", base))

            async def finalize_exit() -> None:
                await core.mutate_task_meta(
                    "task-x", lambda current: {**current, "status": "completed", "exitCode": 0}
                )

            async def annotate() -> None:
                await core.mutate_task_meta(
                    "task-x", lambda current: {**current, "retryOfTaskId": "task-w"}
                )

            async def main() -> None:
                await asyncio.gather(finalize_exit(), annotate())

            run(main())
            meta = run(core.read_task_meta("task-x"))
            self.assertEqual(meta["status"], "completed")
            self.assertEqual(meta["exitCode"], 0)
            self.assertEqual(meta["retryOfTaskId"], "task-w")
        finally:
            self.restore_home(root, previous)

    def test_mutate_task_meta_skips_when_missing_and_mutator_declines(self):
        root, home, workspace, previous = self.temp_context()
        try:
            result = run(core.mutate_task_meta("task-missing", lambda current: None))
            self.assertIsNone(result)
            self.assertIsNone(run(core.read_task_meta("task-missing")))
        finally:
            self.restore_home(root, previous)

    def test_health_check_stop_is_not_resurrected_by_in_flight_tick(self):
        """The stop-vs-tick race: a tick computed before the stop must not
        overwrite the stopped status when it lands after the stop."""
        root, home, workspace, previous = self.temp_context()
        try:
            record = {
                "type": "health_check",
                "taskId": "hc-1",
                "id": "hc-1",
                "status": "active",
                "startedAt": "2026-06-10T00:00:00.000Z",
                "outputFile": str(home / "tasks" / "hc-1" / "output.log"),
                "intervalMs": 1000,
            }
            run(core.save_health_check_task(record))
            # Simulate the stop landing first.
            run(
                core.mutate_health_check_task(
                    "hc-1",
                    lambda current: {**current, "status": "stopped", "stoppedAt": "2026-06-10T00:00:01.000Z"},
                )
            )

            # A tick that loaded `record` before the stop now tries to write.
            def tick(latest):
                if latest is not None and latest.get("status") not in {"active", "unhealthy", "running"}:
                    return latest
                return {**(latest or record), "status": "active", "lastRunAt": "2026-06-10T00:00:02.000Z"}

            run(core.mutate_health_check_task("hc-1", tick))
            final = run(core.load_health_check_task("hc-1"))
            self.assertEqual(final["status"], "stopped")
            self.assertNotIn("lastRunAt", final)
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
