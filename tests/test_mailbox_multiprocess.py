from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
import unittest
from pathlib import Path

import tess_core._facade as core
from tess_core.tasks.communication import (
    _deliver_mailbox_transaction,
    mark_task_message_delivered,
)
from tests.support.tess_core_case import TessCoreTestCase, run

TASK_ID = "task-mp-mailbox"
PARENT_ID = "80000000-0000-4000-8000-000000000001"
CHILD_ID = "80000000-0000-4000-8000-000000000002"
MESSAGES = 24


def _seed_task(workspace: str) -> None:
    run(
        core.write_task_meta(
            TASK_ID,
            {
                "type": "subagent",
                "taskId": TASK_ID,
                "id": TASK_ID,
                "sessionId": CHILD_ID,
                "session_id": CHILD_ID,
                "parent_session_id": PARENT_ID,
                "status": "running",
                "cwd": workspace,
                "startedAt": "2026-01-01T00:00:00Z",
                "outputFile": core.task_output_path(TASK_ID),
            },
        )
    )


def _claimer(home: str, claimer_id: str, barrier_dir: str, results_dir: str) -> None:
    core.configure_runtime_home(home)
    Path(barrier_dir, f"ready-{claimer_id}").write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 20
    while not Path(barrier_dir, "go").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("claimer barrier timed out")
        time.sleep(0.005)

    async def claim() -> list[str]:
        session = {"id": PARENT_ID, "messages": [], "metadata": {}}
        claimed: list[str] = []
        for _ in range(6):
            delivered = await _deliver_mailbox_transaction(TASK_ID, session, None, "to_parent")
            claimed.extend(str(message["messageId"]) for message in delivered)
            await asyncio.sleep(0.01)
        return claimed

    claimed = asyncio.run(claim())
    Path(results_dir, f"claimed-{claimer_id}.json").write_text(json.dumps(claimed), encoding="utf-8")


class MultiProcessMailboxTests(TessCoreTestCase):
    def test_concurrent_process_drains_deliver_each_message_exactly_once(self):
        """The suite tested exactly-once delivery only via asyncio.gather,
        where the in-process lock trivially passes it. This is the real
        contract: multiple PROCESSES draining one mailbox concurrently must
        deliver every message exactly once between them (flock-level claim)."""
        root, home, workspace, previous = self.temp_context()
        processes: list[multiprocessing.Process] = []
        try:
            _seed_task(str(workspace))
            expected = set()
            for index in range(MESSAGES):
                record = run(
                    core.sendTaskMessage(
                        TASK_ID,
                        {"direction": "to_parent", "content": f"result {index:02d}"},
                    )
                )
                expected.add(record["messageId"])
            barrier = Path(root.name, "mailbox-barrier")
            barrier.mkdir()
            results = Path(root.name, "mailbox-results")
            results.mkdir()
            context = multiprocessing.get_context("spawn")
            processes = [
                context.Process(
                    target=_claimer,
                    args=(str(home), f"{index}", str(barrier), str(results)),
                )
                for index in range(4)
            ]
            for process in processes:
                process.start()
            deadline = time.monotonic() + 30
            while len(list(barrier.glob("ready-*"))) < 4:
                self.assertLess(time.monotonic(), deadline, "claimers did not reach barrier")
                time.sleep(0.01)
            (barrier / "go").write_text("go\n", encoding="utf-8")
            for process in processes:
                process.join(30)
                self.assertEqual(process.exitcode, 0)

            all_claims: list[str] = []
            for path in sorted(results.glob("claimed-*.json")):
                all_claims.extend(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(len(all_claims), MESSAGES, "every message delivered exactly once in total")
            self.assertEqual(set(all_claims), expected)
            leftover = [
                message
                for message in run(core.readTaskMailboxMessages(TASK_ID))
                if message.get("status") == "queued"
            ]
            self.assertEqual(leftover, [])
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            self.restore_home(root, previous)

    def test_claimed_reply_is_not_reinjected_by_the_boundary_drain(self):
        """A reply claimed by a waiting tool call must not be re-injected
        into the transcript by the turn's boundary drain."""
        root, home, workspace, previous = self.temp_context()
        try:
            _seed_task(str(workspace))
            record = run(
                core.sendTaskMessage(
                    TASK_ID,
                    {
                        "direction": "to_parent",
                        "content": "the answer",
                        "metadata": {"replyTo": "question-1"},
                    },
                )
            )
            claimed = run(mark_task_message_delivered(TASK_ID, record["messageId"]))
            self.assertEqual(claimed["status"], "delivered")
            session = {"id": PARENT_ID, "messages": [], "metadata": {}}
            delivered = run(_deliver_mailbox_transaction(TASK_ID, session, None, "to_parent"))
            self.assertEqual(delivered, [])
            self.assertEqual(session["messages"], [])
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
