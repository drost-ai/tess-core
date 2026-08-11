from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import agent_core as core
from agent_core.storage.daemon import save_daemon_state
from tests.support.agent_core_case import AgentCoreTestCase, run


class BootSweepInstanceIdentityTests(AgentCoreTestCase):
    def test_boot_sweep_owns_only_its_epoch(self):
        """Bug regression: an instance booting on a shared runtime home must
        not classify another LIVE instance's healthy worker as an orphan and
        kill it. Once the owning instance is dead, the same record must still
        be contained as a dead-epoch orphan."""
        root, home, workspace, previous = self.temp_context()
        proc = None
        supervisor = None
        try:
            task_id = "task-foreign-owned"
            output_file = core.task_output_path(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("still running\n", encoding="utf-8")
            child_argv = [sys.executable, "-c", "import time; time.sleep(30)"]
            proc = subprocess.Popen(
                child_argv,
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            process_identity = run(
                core.captureOwnedProcessIdentity(pid=proc.pid, expectedArgv=child_argv)
            )
            created_at = "2026-01-01T00:00:00Z"
            run(
                core.write_task_meta(
                    task_id,
                    {
                        "type": "background_command",
                        "taskId": task_id,
                        "id": task_id,
                        "sessionId": "session-foreign",
                        "command": "sleep",
                        "cwd": str(workspace),
                        "startedAt": created_at,
                        "updatedAt": created_at,
                        "status": "running",
                        "outputFile": output_file,
                        "pid": proc.pid,
                        "processIdentity": process_identity,
                        "detailStatus": "running",
                        "owner": "daemon",
                        "supervisorId": "peer-supervisor-a",
                    },
                )
            )
            run(
                core.save_task_record(
                    {
                        "id": task_id,
                        "sessionId": "session-foreign",
                        "sourceId": task_id,
                        "kind": "background_command",
                        "status": "running",
                        "title": "sleep",
                        "summary": "Background command is running.",
                        "createdAt": created_at,
                        "updatedAt": created_at,
                        "startedAt": created_at,
                        "cwd": str(workspace),
                        "command": "sleep",
                        "outputFile": output_file,
                        "outputCursor": len("still running\n"),
                        "foregroundMode": "background",
                        "taskFamily": "shell",
                        "detailStatus": "running",
                        "retrieveOnly": True,
                        "owner": "daemon",
                        "supervisorId": "peer-supervisor-a",
                        "processIdentity": process_identity,
                        "metadata": {"pid": proc.pid},
                    }
                )
            )
            # The owning instance is alive: its daemon state records a live
            # pid and the supervisor id stamped on the task.
            run(
                save_daemon_state(
                    "peer-instance-a",
                    {
                        "instanceId": "peer-instance-a",
                        "pid": os.getpid(),
                        "supervisorId": "peer-supervisor-a",
                        "status": "running",
                    },
                )
            )

            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            run(supervisor["close"]())
            supervisor = None
            self.assertIsNone(
                proc.poll(), "boot sweep must not kill a live foreign instance's worker"
            )
            meta = run(core.read_task_meta(task_id))
            self.assertEqual(meta["status"], "running")
            record = run(core.load_task_record(task_id))
            self.assertEqual(record["status"], "running")

            # The owning instance dies: the same record is now a dead-epoch
            # orphan and must be contained.
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            run(
                save_daemon_state(
                    "peer-instance-a",
                    {
                        "instanceId": "peer-instance-a",
                        "pid": dead.pid,
                        "supervisorId": "peer-supervisor-a",
                        "status": "running",
                    },
                )
            )
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            run(supervisor["close"]())
            supervisor = None
            deadline = time.monotonic() + 5
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(
                proc.poll(), "dead-epoch orphan must still be killed by the boot sweep"
            )
            record = run(core.load_task_record(task_id))
            self.assertEqual(record["detailStatus"], "daemon_lost")
            self.assertEqual(record["status"], "stopped")
            meta = run(core.read_task_meta(task_id))
            self.assertEqual(meta["status"], "terminated")
        finally:
            if supervisor is not None:
                run(supervisor["close"]())
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
