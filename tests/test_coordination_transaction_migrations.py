from __future__ import annotations

import asyncio
import multiprocessing
import time
from pathlib import Path
from unittest.mock import patch

import agent_core as core
from tests.support.agent_core_case import AgentCoreTestCase, run
from tests.test_coordination_transactions import (
    BRIDGE_ID,
    PARENT_ID,
    _coordination_writer,
    _create_group,
    _legacy_claim,
    _reconciliation,
)


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

    def test_p03_legacy_live_dead_and_unknown_claimants_migrate_once(self):
        root, home, workspace, previous = self.temp_context()
        try:
            parent_id = "40000000-0000-4000-8000-000000000040"
            live_pid = 41001
            dead_pid = 41002
            for group_id, continuation in (
                (
                    "legacy-live",
                    _legacy_claim(
                        claimantPid=live_pid,
                        claimantStartToken="live-token",
                        claimantPlatform="test",
                    ),
                ),
                (
                    "legacy-dead",
                    _legacy_claim(
                        claimantPid=dead_pid,
                        claimantStartToken="dead-token",
                        claimantPlatform="test",
                    ),
                ),
                (
                    "legacy-unknown-fresh",
                    _legacy_claim(
                        claimedAt="2026-01-01T00:01:50.000Z",
                        startingAt="2026-01-01T00:01:51.000Z",
                    ),
                ),
                ("legacy-unknown-old", _legacy_claim()),
            ):
                run(_create_group(parent_id, group_id, continuation))

            async def observe(pid: int) -> dict:
                if pid == live_pid:
                    return {
                        "pid": live_pid,
                        "processGroupId": live_pid,
                        "startToken": "live-token",
                        "platform": "test",
                        "observedCommandDigest": "live",
                    }
                if pid == dead_pid:
                    raise core.ProcessIdentityError(
                        "process.not_found", "injected dead claimant"
                    )
                raise core.ProcessIdentityError(
                    "process.inspect_unavailable", "injected unknown claimant"
                )

            with patch(
                "agent_core.process.identity._process_identity_observer_for_tests",
                observe,
            ):
                run(core.recoverSubagentLaunchGroupContinuations(
                    parent_id, "2026-01-01T00:02:00.000Z"
                ))
                groups = {
                    group["id"]: group
                    for group in run(core.listSubagentLaunchGroups(parent_id))
                }
                self.assertEqual(
                    groups["legacy-live"]["continuation"]["recoveryReason"],
                    "legacy_claimant_live_grace",
                )
                self.assertEqual(
                    groups["legacy-dead"]["continuation"]["phase"],
                    "retryable",
                )
                self.assertEqual(
                    groups["legacy-unknown-fresh"]["continuation"]["recoveryReason"],
                    "legacy_claimant_unknown_grace",
                )
                self.assertEqual(
                    groups["legacy-unknown-old"]["continuation"]["phase"],
                    "retryable",
                )
                self.assertEqual(
                    run(core.recoverSubagentLaunchGroupContinuations(
                        parent_id, "2026-01-01T00:02:00.000Z"
                    )),
                    [],
                )
                run(core.recoverSubagentLaunchGroupContinuations(
                    parent_id, "2026-01-01T00:03:00.000Z"
                ))
            groups = {
                group["id"]: group
                for group in run(core.listSubagentLaunchGroups(parent_id))
            }
            self.assertEqual(groups["legacy-live"]["continuation"]["phase"], "retryable")
            self.assertEqual(
                groups["legacy-unknown-fresh"]["continuation"]["phase"],
                "retryable",
            )
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
