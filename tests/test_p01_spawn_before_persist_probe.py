from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.support.agent_core_case import AgentCoreTestCase, core, run

from agent_core.process.owned_launch import (
    activate_owned_process_runner,
    create_owned_process_activation,
)


_CRASH_EXIT = 86
_CRASH_BOUNDARIES = (
    "after_reservation",
    "after_session_mutation",
    "after_spawn",
    "after_identity",
    "after_attach",
    "after_activation",
)


def _crashing_launch_worker(
    runtime_home: str,
    workspace: str,
    session_id: str,
    task_id: str,
    boundary: str,
    state_dir: str,
) -> None:
    core.configure_runtime_home(runtime_home)
    state_path = Path(state_dir)

    async def scenario() -> None:
        await core.ensure_base_layout(workspace)
        reservation = core.create_task_launch_reservation(
            taskId=task_id,
            sessionId=session_id,
            kind="background_command",
        )
        output_file = core.task_output_path(task_id)
        meta = {
            "type": "background_command",
            "taskId": task_id,
            "id": task_id,
            "sessionId": session_id,
            "command": "probe-sleep",
            "cwd": workspace,
            "startedAt": reservation["createdAt"],
            "updatedAt": reservation["createdAt"],
            "status": "starting",
            "outputFile": output_file,
            "detailStatus": "starting",
            "owner": "daemon",
            "launchReservation": reservation,
        }
        record = {
            "id": task_id,
            "sessionId": session_id,
            "sourceId": task_id,
            "kind": "background_command",
            "status": "queued",
            "title": boundary,
            "summary": "P01 launch is starting.",
            "createdAt": reservation["createdAt"],
            "updatedAt": reservation["createdAt"],
            "startedAt": reservation["createdAt"],
            "foregroundable": True,
            "restorable": True,
            "cwd": workspace,
            "command": "probe-sleep",
            "outputFile": output_file,
            "detailStatus": "starting",
            "owner": "daemon",
            "launchReservation": reservation,
        }
        activation = create_owned_process_activation(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        holder: dict[str, subprocess.Popen[bytes]] = {}

        async def reserve() -> None:
            await core.write_task_meta(task_id, meta)
            await core.save_task_record(record)
            if boundary == "after_reservation":
                os._exit(_CRASH_EXIT)
            updated = await core.mutate_session_record(
                session_id,
                lambda session: _append_task_id(session, task_id),
            )
            if updated is None:
                raise RuntimeError("P01 session disappeared during reservation")
            if boundary == "after_session_mutation":
                os._exit(_CRASH_EXIT)

        def spawn() -> subprocess.Popen[bytes]:
            env = os.environ.copy()
            env["AGENT_CORE_HOME"] = runtime_home
            process = subprocess.Popen(
                activation["argv"],
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            holder["process"] = process
            (state_path / f"{task_id}.pid").write_text(f"{process.pid}\n", encoding="utf-8")
            if boundary == "after_spawn":
                os._exit(_CRASH_EXIT)
            return process

        async def capture(args):
            identity = await core.capture_owned_process_identity(args)
            (state_path / f"{task_id}.identity.json").write_text(
                json.dumps(identity, sort_keys=True),
                encoding="utf-8",
            )
            if boundary == "after_identity":
                os._exit(_CRASH_EXIT)
            return identity

        async def attach(identity: dict) -> None:
            attached = core.attach_task_launch_reservation(reservation)
            await core.write_task_meta(
                task_id,
                {
                    **meta,
                    "pid": identity["pid"],
                    "processIdentity": identity,
                    "launchReservation": attached,
                    "status": "running",
                    "detailStatus": "launching",
                    "updatedAt": attached["attachedAt"],
                },
            )
            await core.save_task_record(
                {
                    **record,
                    "status": "backgrounded",
                    "summary": "P01 launch attached.",
                    "detailStatus": "launching",
                    "processIdentity": identity,
                    "launchReservation": attached,
                    "updatedAt": attached["attachedAt"],
                }
            )
            if boundary == "after_attach":
                os._exit(_CRASH_EXIT)
            activate_owned_process_runner(
                holder["process"],
                activation["activationToken"],
            )
            if boundary == "after_activation":
                os._exit(_CRASH_EXIT)

        async def fail(error: object) -> None:
            failed = core.fail_task_launch_reservation(reservation, error)
            await core.write_task_meta(
                task_id,
                {
                    **meta,
                    "status": "failed",
                    "detailStatus": "launch_failed",
                    "error": failed["failureReason"],
                    "updatedAt": failed["failedAt"],
                    "finishedAt": failed["failedAt"],
                    "launchReservation": failed,
                },
            )
            await core.save_task_record(
                {
                    **record,
                    "status": "failed",
                    "summary": failed["failureReason"],
                    "detailStatus": "launch_failed",
                    "updatedAt": failed["failedAt"],
                    "completedAt": failed["failedAt"],
                    "launchReservation": failed,
                }
            )

        await core.launch_reserved_process(
            reservation=reservation,
            expectedArgv=activation["argv"],
            reserve=reserve,
            spawn=spawn,
            captureIdentity=capture,
            attach=attach,
            fail=fail,
        )
        raise AssertionError(f"crash boundary was not reached: {boundary}")

    asyncio.run(scenario())


def _append_task_id(session: dict, task_id: str) -> dict:
    metadata = session.setdefault("metadata", {})
    metadata["taskIds"] = list(
        dict.fromkeys([*(metadata.get("taskIds") or []), task_id])
    )
    return session


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_pid_dead(pid: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while _pid_is_alive(pid):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"P01 process {pid} did not exit")
        time.sleep(0.02)


class P01SpawnBeforePersistProbeTests(AgentCoreTestCase):
    def test_real_daemon_crashes_reconcile_every_launch_boundary_exactly_once(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        sentinel: subprocess.Popen[bytes] | None = None
        sentinel_identity: dict | None = None
        state_dir = Path(root.name, "p01-crash-state")
        state_dir.mkdir()
        task_ids = {
            boundary: f"p01-{boundary.replace('_', '-')}"
            for boundary in _CRASH_BOUNDARIES
        }
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                systemPrompt="test",
                permissionMode="bypassPermissions",
                sessionId="p01-crash-session",
            )
            run(core.save_session(session))
            context = multiprocessing.get_context("spawn")
            for boundary, task_id in task_ids.items():
                process = context.Process(
                    target=_crashing_launch_worker,
                    args=(
                        str(home),
                        str(workspace),
                        session["id"],
                        task_id,
                        boundary,
                        str(state_dir),
                    ),
                )
                process.start()
                process.join(15)
                self.assertEqual(process.exitcode, _CRASH_EXIT, boundary)

            sentinel = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            sentinel_identity = run(
                core.capture_owned_process_identity(
                    pid=sentinel.pid,
                    expectedArgv=[sys.executable, "-c", "import time; time.sleep(30)"],
                )
            )
            stale_identity = {**sentinel_identity, "startToken": f"{sentinel_identity['startToken']}:stale"}
            sentinel_task = "p01-reused-pid-sentinel"
            sentinel_reservation = core.create_task_launch_reservation(
                taskId=sentinel_task,
                sessionId=session["id"],
                kind="background_command",
            )
            sentinel_record = _starting_record(
                sentinel_task,
                session["id"],
                str(workspace),
                sentinel_reservation,
            )
            sentinel_record["processIdentity"] = stale_identity
            run(core.write_task_meta(sentinel_task, {"type": "background_command", **sentinel_record}))
            run(core.save_task_record(sentinel_record))

            events: list[dict] = []
            supervisor = run(
                core.create_task_supervisor(
                    {"cwd": str(workspace), "onEvent": lambda event: events.append(event)}
                )
            )
            for boundary in (
                "after_reservation",
                "after_session_mutation",
                "after_spawn",
                "after_identity",
            ):
                record = run(supervisor["inspectTask"](task_ids[boundary]))
                self.assertEqual(record["status"], "failed", boundary)
                self.assertEqual(record["launchReservation"]["state"], "failed", boundary)
                self.assertEqual(
                    record["metadata"]["launch_recovery_state"],
                    "interrupted_before_attachment",
                    boundary,
                )

            attached_id = task_ids["after_attach"]
            _wait_task_terminal(supervisor, attached_id)
            active_id = task_ids["after_activation"]
            active_record = run(supervisor["inspectTask"](active_id))
            self.assertEqual(active_record["status"], "stopped")
            self.assertEqual(active_record["detailStatus"], "daemon_lost")
            self.assertIn("process_kill_outcome", active_record["metadata"])

            stale_record = run(supervisor["inspectTask"](sentinel_task))
            self.assertEqual(stale_record["status"], "failed")
            self.assertEqual(
                stale_record["metadata"]["process_identity_error"],
                "process.identity_mismatch",
            )
            self.assertIsNone(sentinel.poll())

            expected_ids = {*task_ids.values(), sentinel_task}
            persisted = run(core.load_session_record_shallow(session["id"]))
            self.assertEqual(set(persisted["metadata"]["taskIds"]), expected_ids)
            self.assertEqual(len(persisted["metadata"]["taskIds"]), len(expected_ids))
            event_keys = [
                (event.get("event"), event.get("data", {}).get("task_id"))
                for event in events
                if event.get("data", {}).get("task_id") in expected_ids
            ]
            self.assertEqual(len(event_keys), len(set(event_keys)))

            run(supervisor["close"]())
            supervisor = None
            restart_events: list[dict] = []
            supervisor = run(
                core.create_task_supervisor(
                    {
                        "cwd": str(workspace),
                        "onEvent": lambda event: restart_events.append(event),
                    }
                )
            )
            self.assertFalse(
                any(
                    event.get("data", {}).get("task_id") in expected_ids
                    for event in restart_events
                )
            )
            persisted = run(core.load_session_record_shallow(session["id"]))
            self.assertEqual(len(persisted["metadata"]["taskIds"]), len(expected_ids))

            for boundary in ("after_spawn", "after_identity", "after_attach", "after_activation"):
                pid = int((state_dir / f"{task_ids[boundary]}.pid").read_text(encoding="utf-8"))
                _wait_pid_dead(pid)
        finally:
            if supervisor:
                run(supervisor["close"]())
            if sentinel_identity is not None and sentinel is not None and sentinel.poll() is None:
                try:
                    run(core.signal_owned_process(sentinel_identity, signal.SIGTERM))
                except Exception:
                    sentinel.terminate()
                sentinel.wait(timeout=5)
            self.restore_home(root, previous)

def _starting_record(
    task_id: str,
    session_id: str,
    workspace: str,
    reservation: dict,
) -> dict:
    return {
        "id": task_id,
        "taskId": task_id,
        "sessionId": session_id,
        "sourceId": task_id,
        "kind": "background_command",
        "status": "queued",
        "title": task_id,
        "summary": "P01 launch is starting.",
        "createdAt": reservation["createdAt"],
        "updatedAt": reservation["createdAt"],
        "startedAt": reservation["createdAt"],
        "foregroundable": True,
        "restorable": True,
        "cwd": workspace,
        "command": "probe-sleep",
        "outputFile": core.task_output_path(task_id),
        "detailStatus": "starting",
        "owner": "daemon",
        "launchReservation": reservation,
    }


def _wait_task_terminal(supervisor: dict, task_id: str, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        record = run(supervisor["inspectTask"](task_id))
        if record and record.get("status") in {"completed", "failed", "stopped"}:
            return record
        if time.monotonic() >= deadline:
            raise TimeoutError(f"P01 task {task_id} did not become terminal")
        time.sleep(0.05)
