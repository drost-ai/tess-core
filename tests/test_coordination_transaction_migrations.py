from __future__ import annotations

import asyncio
import multiprocessing
import time
from pathlib import Path

import agent_core._facade as core
from tests.support.agent_core_case import AgentCoreTestCase, run

BRIDGE_ID = "30000000-0000-4000-8000-000000000001"
PARENT_ID = "30000000-0000-4000-8000-000000000002"


def _coordination_writer(home: str, writer_id: str, barrier_dir: str) -> None:
    core.configure_runtime_home(home)
    Path(barrier_dir, f"ready-{writer_id}").write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 20
    while not Path(barrier_dir, "go").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("coordination barrier timed out")
        time.sleep(0.005)

    async def write() -> None:
        created_at = "2026-01-01T00:00:00.000Z"
        await asyncio.gather(
            core.append_bridge_inbox_message({
                "id": f"message-{writer_id}",
                "bridgeId": BRIDGE_ID,
                "createdAt": created_at,
                "source": "local",
                "text": f"writer {writer_id}",
            }),
            core.append_bridge_event({
                "id": f"event-{writer_id}",
                "bridgeId": BRIDGE_ID,
                "createdAt": created_at,
                "event": "test.writer",
                "data": {"writer_id": writer_id},
            }),
            core.upsertSubagentReconciliation({
                "id": f"reconciliation-{writer_id}",
                "idempotencyKey": f"writer:{writer_id}",
                "parentSessionId": PARENT_ID,
                "childSessionId": f"child-session-{writer_id}",
                "childAgentId": f"child-agent-{writer_id}",
                "taskId": f"task-{writer_id}",
                "terminalStatus": "completed",
                "summary": f"writer {writer_id}",
                "childCompletedAt": created_at,
                "createdAt": created_at,
            }),
        )

    asyncio.run(write())


def _reconciliation(parent_id: str, suffix: str) -> dict:
    return {
        "id": f"reconciliation-{suffix}",
        "idempotencyKey": f"key-{suffix}",
        "parentSessionId": parent_id,
        "childSessionId": f"child-session-{suffix}",
        "childAgentId": f"child-agent-{suffix}",
        "taskId": f"task-{suffix}",
        "terminalStatus": "completed",
        "summary": "done",
        "childCompletedAt": "2026-01-01T00:00:00.000Z",
        "createdAt": "2026-01-01T00:00:00.000Z",
    }


class CoordinationTransactionMigrationTests(AgentCoreTestCase):
    def test_fifty_process_writers_retain_every_append_and_upsert(self):
        root, home, workspace, previous = self.temp_context()
        processes: list[multiprocessing.Process] = []
        try:
            barrier = Path(root.name, "coordination-barrier")
            barrier.mkdir()
            context = multiprocessing.get_context("spawn")
            processes = [
                context.Process(
                    target=_coordination_writer,
                    args=(str(home), f"{index:02d}", str(barrier)),
                )
                for index in range(50)
            ]
            for process in processes:
                process.start()
            deadline = time.monotonic() + 30
            while len(list(barrier.glob("ready-*"))) < 50:
                self.assertLess(time.monotonic(), deadline, "writers did not reach barrier")
                time.sleep(0.01)
            (barrier / "go").write_text("go\n", encoding="utf-8")
            for process in processes:
                process.join(30)
                self.assertEqual(process.exitcode, 0)

            inbox = run(core.load_bridge_inbox(BRIDGE_ID))
            events = run(core.load_bridge_events(BRIDGE_ID))
            reconciliations = run(core.listSubagentReconciliations(PARENT_ID))
            self.assertEqual(len(inbox), 50)
            self.assertEqual(len({entry["id"] for entry in inbox}), 50)
            self.assertEqual(len(events), 50)
            self.assertEqual(len({entry["id"] for entry in events}), 50)
            self.assertEqual(len(reconciliations), 50)
            self.assertEqual(len({entry["idempotencyKey"] for entry in reconciliations}), 50)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            self.restore_home(root, previous)

    def test_concurrent_parent_delivery_has_one_claim_and_one_consumption(self):
        root, home, workspace, previous = self.temp_context()
        try:
            parent_id = "50000000-0000-4000-8000-000000000002"
            run(core.upsertSubagentReconciliation(_reconciliation(parent_id, "delivery")))
            sessions = [
                {"id": parent_id, "messages": [], "metadata": {}},
                {"id": parent_id, "messages": [], "metadata": {}},
            ]

            async def deliver() -> list[dict]:
                return await asyncio.gather(*(
                    core.appendPendingSubagentReconciliationsToSession(session)
                    for session in sessions
                ))

            deliveries = run(deliver())
            winners = [delivery for delivery in deliveries if delivery["records"]]
            self.assertEqual(len(winners), 1)
            consumed = run(core.consumeAppendedSubagentReconciliations(
                sessions[deliveries.index(winners[0])], winners[0]
            ))
            self.assertEqual(len(consumed), 1)
            self.assertEqual(sum(len(session["messages"]) for session in sessions), 1)
            self.assertIsNotNone(run(core.listSubagentReconciliations(parent_id))[0].get("consumedAt"))
        finally:
            self.restore_home(root, previous)
