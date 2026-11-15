from __future__ import annotations

import asyncio
import sys

from tests.support.tess_core_case import TessCoreTestCase, core, run
from tests.test_p01_spawn_before_persist_probe import (
    _starting_record,
    _wait_pid_dead,
)


class P01LaunchConcurrencyTests(TessCoreTestCase):
    def test_reservation_spawn_and_session_failure_boundaries_do_not_orphan(self):
        trace: list[str] = []
        reservation = core.create_task_launch_reservation(
            taskId="p01-reserve-failure",
            sessionId="p01-boundary-session",
            kind="background_command",
        )

        async def reserve_failure() -> None:
            trace.append("reserve")
            raise RuntimeError("injected reservation write failure")

        async def unexpected_attach(_identity: dict) -> None:
            trace.append("attach")

        async def failed(_error: object) -> None:
            trace.append("fail")

        with self.assertRaisesRegex(RuntimeError, "reservation write failure"):
            run(
                core.launch_reserved_process(
                    reservation=reservation,
                    expectedArgv=[sys.executable, "-c", "pass"],
                    reserve=reserve_failure,
                    spawn=lambda: trace.append("spawn"),
                    attach=unexpected_attach,
                    fail=failed,
                )
            )
        self.assertEqual(trace, ["reserve"])

        trace.clear()
        reservation = core.create_task_launch_reservation(
            taskId="p01-spawn-failure",
            sessionId="p01-boundary-session",
            kind="background_command",
        )

        def spawn_failure():
            trace.append("spawn")
            raise OSError("injected spawn failure")

        with self.assertRaisesRegex(OSError, "spawn failure"):
            run(
                core.launch_reserved_process(
                    reservation=reservation,
                    expectedArgv=[sys.executable, "-c", "pass"],
                    reserve=lambda: _append_async(trace, "reserve"),
                    spawn=spawn_failure,
                    attach=unexpected_attach,
                    fail=failed,
                )
            )
        self.assertEqual(trace, ["reserve", "spawn", "fail"])

        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                systemPrompt="test",
                permissionMode="bypassPermissions",
                sessionId="p01-session-failure",
            )
            run(core.save_session(session))
            task_id = "p01-session-mutation-failure"
            reservation = core.create_task_launch_reservation(
                taskId=task_id,
                sessionId=session["id"],
                kind="background_command",
            )
            record = _starting_record(task_id, session["id"], str(workspace), reservation)
            spawned = False

            async def partial_reserve() -> None:
                await core.write_task_meta(task_id, {"type": "background_command", **record})
                await core.save_task_record(record)
                raise RuntimeError("injected session mutation failure")

            def unexpected_spawn():
                nonlocal spawned
                spawned = True
                raise AssertionError("spawn must not run")

            with self.assertRaisesRegex(RuntimeError, "session mutation failure"):
                run(
                    core.launch_reserved_process(
                        reservation=reservation,
                        expectedArgv=[sys.executable, "-c", "pass"],
                        reserve=partial_reserve,
                        spawn=unexpected_spawn,
                        attach=unexpected_attach,
                        fail=failed,
                    )
                )
            self.assertFalse(spawned)
            supervisor = run(core.create_task_supervisor({"cwd": str(workspace)}))
            reconciled = run(supervisor["inspectTask"](task_id))
            persisted = run(core.load_session_record_shallow(session["id"]))
            self.assertEqual(reconciled["status"], "failed")
            self.assertEqual(reconciled["launchReservation"]["state"], "failed")
            self.assertEqual(persisted["metadata"]["taskIds"], [task_id])
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_twenty_five_concurrent_real_process_starts_are_exactly_once(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        identities: list[dict] = []
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                systemPrompt="test",
                permissionMode="bypassPermissions",
                sessionId="p01-twenty-five-session",
            )
            run(core.save_session(session))
            supervisor = run(core.create_task_supervisor({"cwd": str(workspace)}))

            async def launch_all() -> list[dict]:
                return await asyncio.gather(
                    *(
                        supervisor["startBackgroundCommand"](
                            {
                                "sessionId": session["id"],
                                "command": "exec sleep 30",
                                "cwd": str(workspace),
                                "title": f"P01 start {index:02d}",
                            }
                        )
                        for index in range(25)
                    )
                )

            starts = run(launch_all())
            task_ids = [start["taskId"] for start in starts]
            self.assertEqual(len(task_ids), 25)
            self.assertEqual(len(set(task_ids)), 25)
            persisted = run(core.load_session_record_shallow(session["id"]))
            self.assertEqual(set(persisted["metadata"]["taskIds"]), set(task_ids))
            self.assertEqual(len(persisted["metadata"]["taskIds"]), 25)
            records = {
                record["id"]: record
                for record in run(core.list_task_records())
                if record.get("sessionId") == session["id"]
            }
            self.assertEqual(set(records), set(task_ids))
            for task_id in task_ids:
                self.assertEqual(records[task_id]["launchReservation"]["state"], "attached")
                meta = run(core.read_task_meta(task_id))
                self.assertEqual(meta["launchReservation"]["state"], "attached")
                self.assertEqual(meta["processIdentity"], records[task_id]["processIdentity"])
                identities.append(meta["processIdentity"])

            async def stop_all() -> None:
                await asyncio.gather(*(supervisor["stopTask"](task_id) for task_id in task_ids))

            run(stop_all())
            for identity in identities:
                _wait_pid_dead(identity["pid"])
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)


async def _append_async(values: list[str], value: str) -> None:
    values.append(value)
