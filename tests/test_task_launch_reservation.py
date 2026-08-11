from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

from tests.support.agent_core_case import *


async def _wait_dead(pid: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"process {pid} did not exit")
        await asyncio.sleep(0.02)


class TaskLaunchReservationTests(AgentCoreTestCase):
    def test_reservation_is_durable_before_spawn_and_identity_attaches_before_return(self):
        async def scenario():
            trace = []
            reservation = core.create_task_launch_reservation(
                taskId="task-reserved", sessionId="session-owner", kind="background_command"
            )
            launched = await core.launch_reserved_process(
                reservation=reservation,
                expectedArgv=["/bin/sh", "-c", "sleep 30"],
                reserve=lambda: _append_async(trace, "reserved"),
                spawn=lambda: _spawn_sleep(trace),
                attach=lambda identity: _append_async(trace, f"attached:{identity['pid']}"),
                fail=lambda _error: _append_async(trace, "failed"),
            )
            try:
                self.assertEqual(trace[:2], ["reserved", "spawned"])
                self.assertTrue(trace[2].startswith("attached:"))
                self.assertIsNotNone(launched.identity)
                await core.signal_owned_process(launched.identity)
                await launched.completion
            finally:
                if launched.child.poll() is None:
                    launched.child.terminate()
                    await _wait_dead(launched.child.pid)

        run(scenario())

    def test_attach_failure_drains_owned_process_and_marks_failed(self):
        async def scenario():
            state = {"failed": False, "child": None}
            reservation = core.create_task_launch_reservation(
                taskId="task-rollback", sessionId="session-owner", kind="background_command"
            )

            def spawn():
                child = subprocess.Popen(
                    ["/bin/sh", "-c", "sleep 30"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                state["child"] = child
                return child

            async def attach(_identity):
                raise RuntimeError("injected attach persistence failure")

            async def fail(_error):
                state["failed"] = True

            with self.assertRaisesRegex(RuntimeError, "attach persistence"):
                await core.launch_reserved_process(
                    reservation=reservation,
                    expectedArgv=["/bin/sh", "-c", "sleep 30"],
                    reserve=lambda: _append_async([], "reserved"),
                    spawn=spawn,
                    attach=attach,
                    fail=fail,
                )
            self.assertTrue(state["failed"])
            await _wait_dead(state["child"].pid)

        run(scenario())

    def test_fast_exit_before_identity_attachment_is_failed_and_recoverable(self):
        async def scenario():
            trace = []
            reservation = core.create_task_launch_reservation(
                taskId="task-fast-exit", sessionId="session-owner", kind="background_command"
            )

            async def missing(_args):
                await asyncio.sleep(0.1)
                raise core.ProcessIdentityError("process.not_found", "process exited")

            launched = await core.launch_reserved_process(
                reservation=reservation,
                expectedArgv=[sys.executable, "-c", "raise SystemExit(0)"],
                reserve=lambda: _append_async(trace, "reserved"),
                spawn=lambda: subprocess.Popen(
                    [sys.executable, "-c", "raise SystemExit(0)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                ),
                captureIdentity=missing,
                attach=lambda _identity: _append_async(trace, "attached"),
                fail=lambda error: _append_async(trace, f"failed:{error.code}"),
            )
            self.assertEqual(trace, ["reserved", "failed:process.not_found"])
            self.assertIsNone(launched.identity)
            self.assertEqual(launched.completedBeforeAttach, {"type": "exit", "code": 0, "signal": None})

        run(scenario())

    def test_p01_spawn_before_persist_preserves_concurrent_session_mutations(self):
        root, home, workspace, previous = self.temp_context()

        async def scenario():
            core.configure_runtime_home(str(home))
            await core.ensure_base_layout(str(workspace))
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                systemPrompt="test",
                permissionMode="default",
                sessionId="session-p01",
                name="p01 sentinel",
            )
            session["messages"] = [{"role": "user", "content": "preserve"}]
            session["metadata"]["taskIds"] = []
            await core.save_session(session)
            first, second = "task-p01-first", "task-p01-second"
            reservation = core.create_task_launch_reservation(
                taskId=first, sessionId=session["id"], kind="background_command"
            )
            attach_reached = asyncio.Event()
            release_attach = asyncio.Event()
            child_holder = {}

            async def reserve():
                now = reservation["createdAt"]
                await core.save_task_record(
                    {
                        "id": first,
                        "sessionId": session["id"],
                        "sourceId": first,
                        "kind": "background_command",
                        "status": "queued",
                        "title": "P01",
                        "createdAt": now,
                        "updatedAt": now,
                        "startedAt": now,
                        "foregroundable": True,
                        "restorable": True,
                        "launchReservation": reservation,
                    }
                )
                await core.mutate_session_record(
                    session["id"],
                    lambda current: _append_session_task(current, first),
                )

            def spawn():
                child = subprocess.Popen(
                    ["/bin/sh", "-c", "sleep 30"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                child_holder["child"] = child
                return child

            async def attach(identity):
                attach_reached.set()
                await release_attach.wait()
                current = await core.load_task_record(first)
                await core.save_task_record(
                    {
                        **current,
                        "status": "backgrounded",
                        "processIdentity": identity,
                        "launchReservation": core.attach_task_launch_reservation(reservation),
                    }
                )

            launch = asyncio.create_task(
                core.launch_reserved_process(
                    reservation=reservation,
                    expectedArgv=["/bin/sh", "-c", "sleep 30"],
                    reserve=reserve,
                    spawn=spawn,
                    attach=attach,
                    fail=lambda _error: _append_async([], "failed"),
                )
            )
            await attach_reached.wait()
            starting = await core.load_task_record(first)
            self.assertEqual(starting["status"], "queued")
            self.assertNotIn("processIdentity", starting)
            await core.mutate_session_record(
                session["id"],
                lambda current: _append_session_task(current, second),
            )
            release_attach.set()
            launched = await launch
            try:
                persisted = await core.load_session(session["id"])
                self.assertEqual(set(persisted["metadata"]["taskIds"]), {first, second})
                self.assertEqual(persisted["name"], "p01 sentinel")
                self.assertEqual(persisted["messages"], [{"role": "user", "content": "preserve"}])
                attached = await core.load_task_record(first)
                self.assertEqual(attached["launchReservation"]["state"], "attached")
                self.assertEqual(attached["processIdentity"], launched.identity)
            finally:
                if launched.identity:
                    await core.signal_owned_process(launched.identity, signal.SIGTERM)
                await _wait_dead(child_holder["child"].pid)

        try:
            run(scenario())
        finally:
            self.restore_home(root, previous)


async def _append_async(target, value):
    target.append(value)


def _spawn_sleep(trace):
    trace.append("spawned")
    return subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _append_session_task(current, task_id):
    metadata = current.setdefault("metadata", {})
    metadata["taskIds"] = list(dict.fromkeys([*(metadata.get("taskIds") or []), task_id]))
    return current
