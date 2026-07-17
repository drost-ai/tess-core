from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_core as core
from tests.support.agent_core_case import AgentCoreTestCase, run


BRIDGE_ID = "30000000-0000-4000-8000-000000000001"
PARENT_ID = "30000000-0000-4000-8000-000000000002"


def _coordination_writer(home: str, writer_id: str, barrier_dir: str) -> None:
    core.configureRuntimeHome(home)
    Path(barrier_dir, f"ready-{writer_id}").write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 20
    while not Path(barrier_dir, "go").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("coordination barrier timed out")
        time.sleep(0.005)

    async def write() -> None:
        created_at = "2026-01-01T00:00:00.000Z"
        await asyncio.gather(
            core.appendBridgeInboxMessage({
                "id": f"message-{writer_id}",
                "bridgeId": BRIDGE_ID,
                "createdAt": created_at,
                "source": "local",
                "text": f"writer {writer_id}",
            }),
            core.appendBridgeEvent({
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


def _continuation_crash_worker(
    home: str,
    stage: str,
    parent_id: str,
    record_id: str,
) -> None:
    core.configureRuntimeHome(home)

    async def crash() -> None:
        if stage == "before_parent_completion_consumption":
            claim = await core.claimUnconsumedSubagentReconciliations(
                parentSessionId=parent_id,
                consumerId=f"crash-worker:{os.getpid()}",
                leaseMs=25,
            )
            if not claim:
                raise RuntimeError("could not claim reconciliation")
            os._exit(86)
        claim = await core.claimSubagentLaunchGroupContinuation(
            parentSessionId=parent_id,
            launchGroupId=record_id,
            claimantId=f"crash-worker:{os.getpid()}",
            leaseMs=25,
        )
        if not claim:
            raise RuntimeError("could not claim continuation")
        if stage == "after_claim":
            os._exit(86)
        starting = await core.markSubagentLaunchGroupContinuationStarting(
            parent_id, record_id, claim["claimToken"]
        )
        if not starting:
            raise RuntimeError("could not persist pre-start")
        if stage == "after_pre_start":
            os._exit(86)
        if stage == "after_durable_child":
            await core.saveTaskRecord(_task_record(parent_id, record_id, record_id))
            os._exit(86)
        raise RuntimeError(f"unknown stage: {stage}")

    asyncio.run(crash())


class CoordinationTransactionTests(AgentCoreTestCase):
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

            inbox = run(core.loadBridgeInbox(BRIDGE_ID))
            events = run(core.loadBridgeEvents(BRIDGE_ID))
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

    def test_p03_claims_recover_before_and_after_durable_child(self):
        root, home, workspace, previous = self.temp_context()
        try:
            parent_id = "40000000-0000-4000-8000-000000000001"
            marker = {"schemaVersion": 1, "pid": os.getpid(), "startToken": "test", "platform": "test"}
            with patch("agent_core.subagent_coordination.captureProcessBirthMarker", AsyncMock(return_value=marker)):
                run(_create_group(parent_id, "dual-claim"))

                async def dual_claims() -> list[dict | None]:
                    return await asyncio.gather(*(
                        core.claimSubagentLaunchGroupContinuation(
                            parentSessionId=parent_id,
                            launchGroupId="dual-claim",
                            claimantId=f"claimant-{index}",
                        )
                        for index in range(25)
                    ))

                claims = run(dual_claims())
                winners = [claim for claim in claims if claim]
                self.assertEqual(len(winners), 1)
                run(core.releaseSubagentLaunchGroupContinuationClaim(
                    parent_id, "dual-claim", winners[0]["claimToken"], "pre-start crash"
                ))
                retry = run(core.claimSubagentLaunchGroupContinuation(
                    parentSessionId=parent_id,
                    launchGroupId="dual-claim",
                    claimantId="retry",
                    now="2026-01-01T00:00:00.000Z",
                    leaseMs=25,
                ))
                self.assertIsNotNone(retry)
                run(core.markSubagentLaunchGroupContinuationStarting(
                    parent_id,
                    "dual-claim",
                    retry["claimToken"],
                    "2026-01-01T00:00:00.000Z",
                ))
            run(core.recoverSubagentLaunchGroupContinuations(parent_id, "2026-01-01T00:01:00.000Z"))
            recovered_claim = run(core.claimSubagentLaunchGroupContinuation(
                parentSessionId=parent_id,
                launchGroupId="dual-claim",
                claimantId="post-crash",
                now="2026-01-01T00:01:00.000Z",
            ))
            self.assertIsNotNone(recovered_claim)

            durable_id = "40000000-0000-4000-8000-000000000010"
            run(_create_group(parent_id, "durable-child", {
                "schemaVersion": 1,
                "phase": "starting",
                "attempt": 1,
                "claimToken": "crashed",
                "leaseExpiresAt": "2026-01-01T00:00:30.000Z",
            }))
            run(core.saveTaskRecord(_task_record(parent_id, durable_id, "durable-child")))
            run(core.recoverSubagentLaunchGroupContinuations(parent_id, "2026-01-01T00:01:00.000Z"))
            durable = next(group for group in run(core.listSubagentLaunchGroups(parent_id)) if group["id"] == "durable-child")
            self.assertEqual(durable["continuation"]["phase"], "running")
            self.assertEqual(durable["continuation"]["childTaskId"], durable_id)
            self.assertIsNone(run(core.claimSubagentLaunchGroupContinuation(
                parentSessionId=parent_id,
                launchGroupId="durable-child",
                claimantId="must-not-duplicate",
            )))
            completed = run(core.markSubagentLaunchGroupContinuationCompleted(
                parent_id, "durable-child", durable_id
            ))
            repeated = run(core.markSubagentLaunchGroupContinuationCompleted(
                parent_id, "durable-child", durable_id
            ))
            self.assertEqual(completed["continuation"]["completedAt"], repeated["continuation"]["completedAt"])
        finally:
            self.restore_home(root, previous)

    def test_p03_real_process_crashes_recover_and_consumption_is_exactly_once(self):
        root, home, workspace, previous = self.temp_context()
        try:
            context = multiprocessing.get_context("spawn")
            parent_id = "40000000-0000-4000-8000-000000000020"
            future = "2099-01-01T00:00:00.000Z"
            for index, stage in enumerate(("after_claim", "after_pre_start"), start=1):
                group_id = f"40000000-0000-4000-8000-{index:012d}"
                run(_create_group(parent_id, group_id))
                process = context.Process(
                    target=_continuation_crash_worker,
                    args=(str(home), stage, parent_id, group_id),
                )
                process.start()
                process.join(20)
                self.assertEqual(process.exitcode, 86)
                run(core.recoverSubagentLaunchGroupContinuations(parent_id, future))
                group = next(item for item in run(core.listSubagentLaunchGroups(parent_id)) if item["id"] == group_id)
                self.assertEqual(group["continuation"]["phase"], "retryable")

            durable_id = "40000000-0000-4000-8000-000000000033"
            run(_create_group(parent_id, durable_id))
            process = context.Process(
                target=_continuation_crash_worker,
                args=(str(home), "after_durable_child", parent_id, durable_id),
            )
            process.start()
            process.join(20)
            self.assertEqual(process.exitcode, 86)
            run(core.recoverSubagentLaunchGroupContinuations(parent_id, future))
            durable = next(item for item in run(core.listSubagentLaunchGroups(parent_id)) if item["id"] == durable_id)
            self.assertEqual(durable["continuation"]["phase"], "running")
            self.assertEqual(durable["continuation"]["childTaskId"], durable_id)

            run(core.upsertSubagentReconciliation(_reconciliation(parent_id, "crashed-consumer")))
            process = context.Process(
                target=_continuation_crash_worker,
                args=(str(home), "before_parent_completion_consumption", parent_id, "unused"),
            )
            process.start()
            process.join(20)
            self.assertEqual(process.exitcode, 86)
            reclaimed = run(core.claimUnconsumedSubagentReconciliations(
                parentSessionId=parent_id,
                consumerId="recovery",
                now=future,
            ))
            self.assertEqual(len(reclaimed["records"]), 1)
            self.assertEqual(len(run(core.completeSubagentReconciliationClaim(
                parent_id, reclaimed["claimToken"]
            ))), 1)
            self.assertIsNone(run(core.claimUnconsumedSubagentReconciliations(
                parentSessionId=parent_id,
                consumerId="must-not-repeat",
                now="2099-01-01T00:01:00.000Z",
            )))
        finally:
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


async def _create_group(parent_id: str, group_id: str, continuation: dict | None = None) -> dict:
    return await core.upsertSubagentLaunchGroup({
        "id": group_id,
        "parentSessionId": parent_id,
        "childAgentIds": ["child"],
        "taskIds": ["task"],
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
        "continuation": continuation,
    })


def _task_record(parent_id: str, task_id: str, group_id: str) -> dict:
    return {
        "id": task_id,
        "sessionId": parent_id,
        "sourceId": task_id,
        "kind": "main_session",
        "status": "backgrounded",
        "title": "continuation",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
        "startedAt": "2026-01-01T00:00:00.000Z",
        "foregroundable": True,
        "restorable": True,
        "metadata": {"parent_continuation": True, "launch_group_id": group_id},
    }


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
