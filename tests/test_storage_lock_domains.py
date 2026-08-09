from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import unittest

import agent_core as core
from tests.support.agent_core_case import AgentCoreTestCase, run

from tests.test_storage_concurrency import REPO_ROOT


class TaskLifecycleLockDomainTests(AgentCoreTestCase):
    def test_lifecycle_transaction_excludes_bare_task_meta_writer(self):
        """One lock domain per task: a bare task-meta write must wait for a
        held task-lifecycle transaction instead of landing inside its
        read-modify-write window and being clobbered by the stale commit."""
        root, home, workspace, previous = self.temp_context()
        try:
            from agent_core.storage import withRecordTransaction

            base = {
                "type": "background_command",
                "taskId": "task-x",
                "id": "task-x",
                "status": "running",
                "startedAt": "2026-06-10T00:00:00.000Z",
                "outputFile": "/tmp/out.log",
            }
            run(core.writeTaskMeta("task-x", base))

            async def main() -> None:
                in_window = asyncio.Event()
                proceed = asyncio.Event()

                async def lifecycle_transaction() -> None:
                    async with withRecordTransaction("task-lifecycle", "task-x"):
                        stale = await core.readTaskMeta("task-x")
                        in_window.set()
                        await proceed.wait()
                        await asyncio.sleep(0.2)
                        await core.writeTaskMeta("task-x", {**stale, "holder": True})

                async def bare_writer() -> None:
                    await in_window.wait()
                    proceed.set()
                    await core.mutateTaskMeta("task-x", lambda cur: {**cur, "contender": True})

                await asyncio.gather(lifecycle_transaction(), bare_writer())

            run(main())
            meta = run(core.readTaskMeta("task-x"))
            self.assertTrue(meta.get("holder"))
            self.assertTrue(meta.get("contender"))
        finally:
            self.restore_home(root, previous)

    def test_lifecycle_transaction_excludes_bare_writer_across_processes(self):
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
            run(core.writeTaskMeta("task-x", base))
            holder_script = textwrap.dedent(
                """
                import asyncio, sys
                sys.path.insert(0, sys.argv[2])
                import agent_core as core
                from agent_core.storage import withRecordTransaction

                async def main():
                    core.configureRuntimeHome(sys.argv[1])
                    async with withRecordTransaction("task-lifecycle", "task-x"):
                        stale = await core.readTaskMeta("task-x")
                        print("locked", flush=True)
                        await asyncio.sleep(1.0)
                        await core.writeTaskMeta("task-x", {**stale, "holder": True})

                asyncio.run(main())
                """
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_script, str(home), REPO_ROOT],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(holder.stdout.readline().strip(), "locked")

            async def bare_writer() -> None:
                await core.mutateTaskMeta("task-x", lambda cur: {**cur, "contender": True})

            run(bare_writer())
            _, stderr = holder.communicate(timeout=30)
            self.assertEqual(holder.returncode, 0, stderr)
            meta = run(core.readTaskMeta("task-x"))
            self.assertTrue(meta.get("holder"))
            self.assertTrue(meta.get("contender"))
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
