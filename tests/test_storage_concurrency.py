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
            result = run(core.mutateJsonFile(path, lambda value: {**value, "a": 1}, {}))
            self.assertEqual(result, {"a": 1})
            self.assertEqual(json.loads(Path(path).read_text()), {"a": 1})
        finally:
            self.restore_home(root, previous)

    def test_in_place_mutator_returning_none_persists_received_value(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "value.json")
            run(core.writeJsonFile(path, {"items": []}))

            def add(value):
                value["items"].append("x")

            result = run(core.mutateJsonFile(path, add, {}))
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
            run(core.writeJsonFile(plain, value))
            run(core.mutateJsonFile(locked, lambda _: value, {}))
            self.assertEqual(Path(plain).read_bytes(), Path(locked).read_bytes())
        finally:
            self.restore_home(root, previous)

    def test_concurrent_coroutine_mutations_all_survive(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "shared.json")

            async def writer(index: int) -> None:
                await core.mutateJsonFile(path, lambda value: {**value, f"key{index}": index}, {})

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
                        await core.mutateJsonFile(
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

    def test_lock_timeout_notifies_observer_and_falls_back(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = str(home / "records" / "contended.json")
            events: list[dict] = []
            unsubscribe = core.registerStorageLockObserver(events.append)
            try:

                async def real_main() -> None:
                    release = asyncio.Event()
                    entered = asyncio.Event()

                    async def holder() -> None:
                        async with core.fileLock(path):
                            entered.set()
                            await release.wait()

                    holder_task = asyncio.create_task(holder())
                    await entered.wait()
                    async with core.fileLock(path, timeoutMs=150):
                        pass
                    release.set()
                    await holder_task

                run(real_main())
            finally:
                unsubscribe()
            self.assertTrue(events, "expected a lock-timeout notification")
            self.assertEqual(events[0]["action"], "fallback_unlocked")
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
                    async with core.fileLock(sys.argv[1]):
                        print("locked", flush=True)
                        await asyncio.sleep(1.0)
                        await core.writeJsonFile(sys.argv[1], {"writer": "holder"})

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
                await core.mutateJsonFile(path, lambda value: {**value, "writer2": True}, {})

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
            run(core.writeJsonFile(path, {"state": "old"}))
            crasher = textwrap.dedent(
                """
                import asyncio, os, sys
                sys.path.insert(0, sys.argv[2])
                import agent_core as core
                from agent_core.storage_store import json_io

                real_replace = os.replace

                def exploding_replace(src, dst):
                    os._exit(9)

                async def main():
                    os.replace = exploding_replace
                    await core.mutateJsonFile(sys.argv[1], lambda value: {"state": "new"}, {})

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
            run(core.mutateJsonFile(path, lambda value: {**value, "after": True}, {}))
            self.assertEqual(json.loads(Path(path).read_text()), {"state": "old", "after": True})
        finally:
            self.restore_home(root, previous)

    def test_lock_timeout_event_name_is_known(self):
        from agent_core.runtime_event_parts.shared import KNOWN_RUNTIME_EVENTS

        self.assertIn("storage.lock_timeout", KNOWN_RUNTIME_EVENTS)


if __name__ == "__main__":
    unittest.main()
