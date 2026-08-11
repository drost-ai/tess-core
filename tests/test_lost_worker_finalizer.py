from __future__ import annotations

import os
import subprocess
import sys
import unittest

import agent_core as core
from agent_core.tasks.supervisor_parts.finalize_child import finalizeLostWorker
from agent_core.tasks.supervisor_parts.live_work import _reconcile_dead_task_meta
from tests.support.agent_core_case import AgentCoreTestCase, run

PARENT_ID = "60000000-0000-4000-8000-000000000001"
CHILD_SESSION_ID = "60000000-0000-4000-8000-000000000002"


def _dead_worker_fixture(workspace: str, task_id: str, agent_id: str) -> dict:
    """A subagent whose process died without finalizing: meta+record running,
    child active, pid pointing at a reaped process."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    created_at = "2026-01-01T00:00:00Z"
    output_file = core.task_output_path(task_id)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    meta = {
        "type": "subagent",
        "taskId": task_id,
        "id": task_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "sessionId": CHILD_SESSION_ID,
        "session_id": CHILD_SESSION_ID,
        "parent_session_id": PARENT_ID,
        "cwd": workspace,
        "pid": dead.pid,
        "startedAt": created_at,
        "updatedAt": created_at,
        "status": "running",
        "outputFile": output_file,
        "title": "Dead worker",
        "owner": "daemon",
    }
    run(core.write_task_meta(task_id, meta))
    run(
        core.save_task_record(
            {
                "id": task_id,
                "sessionId": PARENT_ID,
                "sourceId": agent_id,
                "kind": "subagent",
                "status": "running",
                "title": "Dead worker",
                "summary": "Background subagent is running.",
                "createdAt": created_at,
                "updatedAt": created_at,
                "startedAt": created_at,
                "cwd": workspace,
                "outputFile": output_file,
                "foregroundMode": "background",
                "taskFamily": "subagent",
                "detailStatus": "worker_running",
                "retrieveOnly": False,
                "owner": "daemon",
                "metadata": {"agent_id": agent_id, "pid": dead.pid},
            }
        )
    )
    run(
        core.save_child_agent(
            {
                "agentId": agent_id,
                "sessionId": CHILD_SESSION_ID,
                "parentSessionId": PARENT_ID,
                "name": "worker",
                "description": "worker",
                "status": "backgrounded",
                "startedAt": created_at,
                "taskId": task_id,
                "pid": dead.pid,
                "runInBackground": True,
            }
        )
    )
    return meta


class LostWorkerFinalizerTests(AgentCoreTestCase):
    def test_live_work_apply_finalizes_record_child_and_parent_ledger(self):
        """Bug regressions: live-work wrote meta but never the task record (a
        dead subagent held a capacity slot until reboot) and no lost path
        wrote the parent-facing reconciliation ledger entry."""
        root, home, workspace, previous = self.temp_context()
        try:
            task_id, agent_id = "task-lw-dead", "agent-lw-dead"
            meta = _dead_worker_fixture(str(workspace), task_id, agent_id)
            next_meta, changed = run(_reconcile_dead_task_meta(task_id, meta, "subagent", apply=True))
            self.assertTrue(changed)
            self.assertEqual(next_meta["status"], "failed")
            record = run(core.load_task_record(task_id))
            self.assertEqual(record["status"], "failed")
            child = run(core.load_child_agent(agent_id))
            self.assertEqual(child["status"], "failed")
            self.assertEqual(child["completionReason"], "worker_lost")
            ledger = run(core.listSubagentReconciliations(PARENT_ID))
            self.assertEqual(len(ledger), 1)
            self.assertEqual(ledger[0]["taskId"], task_id)
            self.assertEqual(ledger[0]["terminalStatus"], "failed")
        finally:
            self.restore_home(root, previous)

    def test_finalizer_honors_first_terminal_wins(self):
        root, home, workspace, previous = self.temp_context()
        try:
            task_id, agent_id = "task-terminal", "agent-terminal"
            _dead_worker_fixture(str(workspace), task_id, agent_id)
            run(
                core.mutate_task_meta(
                    task_id, lambda current: {**current, "status": "completed", "summary": "done"}
                )
            )
            run(
                core.save_task_record(
                    {**run(core.load_task_record(task_id)), "status": "completed", "summary": "done"}
                )
            )
            outcome = run(
                finalizeLostWorker(taskId=task_id, cause="worker_lost", source="test")
            )
            self.assertFalse(outcome["changed"])
            self.assertEqual(run(core.load_task_record(task_id))["status"], "completed")
            self.assertEqual(run(core.read_task_meta(task_id))["status"], "completed")
        finally:
            self.restore_home(root, previous)

    def test_recycled_pid_does_not_pin_dead_worker_records_live(self):
        """A live pid is not proof of a live worker: the meta's pid may have
        been recycled by an unrelated process while the persisted identity
        belongs to the dead worker."""
        root, home, workspace, previous = self.temp_context()
        try:
            task_id, agent_id = "task-recycled", "agent-recycled"
            child_argv = [sys.executable, "-c", "import time; time.sleep(30)"]
            victim = subprocess.Popen(child_argv, start_new_session=True)
            identity = run(core.captureOwnedProcessIdentity(pid=victim.pid, expectedArgv=child_argv))
            victim.kill()
            victim.wait()
            meta = _dead_worker_fixture(str(workspace), task_id, agent_id)
            # Simulate recycling: pid field points at a LIVE process (this
            # test), while the persisted identity belongs to the dead worker.
            meta = run(
                core.mutate_task_meta(
                    task_id,
                    lambda current: {**current, "pid": os.getpid(), "processIdentity": identity},
                )
            )
            next_meta, changed = run(_reconcile_dead_task_meta(task_id, meta, "subagent", apply=True))
            self.assertTrue(changed)
            self.assertEqual(run(core.load_task_record(task_id))["status"], "failed")
        finally:
            self.restore_home(root, previous)

    def test_failure_finalizer_cannot_rewrite_completed_child(self):
        """Bug regression: a success finalizer that completed durably followed
        by a late failure signal rewrote the completed child as failed and
        destroyed the result."""
        root, home, workspace, previous = self.temp_context()
        previous_env = {name: os.environ.get(name) for name in ("TESS_CHILD_AGENT_ID", "TESS_CHILD_TASK_ID", "TESS_CHILD_SESSION_ID")}
        try:
            agent_id, task_id = "agent-done", "task-done"
            run(
                core.save_child_agent(
                    {
                        "agentId": agent_id,
                        "sessionId": CHILD_SESSION_ID,
                        "parentSessionId": PARENT_ID,
                        "name": "done",
                        "description": "done",
                        "status": "completed",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "completedAt": "2026-01-01T00:01:00Z",
                        "resultSummary": "the result",
                        "taskId": task_id,
                        "runInBackground": True,
                    }
                )
            )
            run(
                core.write_task_meta(
                    task_id,
                    {
                        "type": "subagent",
                        "taskId": task_id,
                        "id": task_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "sessionId": CHILD_SESSION_ID,
                        "status": "completed",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "outputFile": core.task_output_path(task_id),
                        "resultSummary": "the result",
                    },
                )
            )
            os.environ["TESS_CHILD_AGENT_ID"] = agent_id
            os.environ["TESS_CHILD_TASK_ID"] = task_id
            os.environ["TESS_CHILD_SESSION_ID"] = CHILD_SESSION_ID
            run(core.finalizeBackgroundSubagentFailureFromEnv(RuntimeError("late failure")))
            child = run(core.load_child_agent(agent_id))
            self.assertEqual(child["status"], "completed")
            self.assertEqual(child["resultSummary"], "the result")
            self.assertNotIn("error", child)
            meta = run(core.read_task_meta(task_id))
            self.assertEqual(meta["status"], "completed")
            self.assertEqual(run(core.listSubagentReconciliations(PARENT_ID)), [])
        finally:
            for name, value in previous_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
