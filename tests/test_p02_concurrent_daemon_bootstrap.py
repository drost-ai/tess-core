from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import queue
import signal
import time
from pathlib import Path
from typing import Any

from tests.support.agent_core_case import AgentCoreTestCase, core, run

from agent_core.daemon.security import (
    daemonTokenId,
    readDaemonAuthMaterial,
    verifyOwnedDaemonSocket,
    verifyPrivateDaemonRuntime,
)


P02_INSTANCE = "p02-test-instance"

_FAULT_STAGES = {
    "pre_bind": {
        "socketExists": False,
        "tokenExists": False,
        "stateExists": False,
        "statePhase": None,
    },
    "post_bind": {
        "socketExists": True,
        "tokenExists": False,
        "stateExists": False,
        "statePhase": None,
    },
    "post_publish": {
        "socketExists": True,
        "tokenExists": True,
        "stateExists": True,
        "statePhase": "starting",
    },
    "during_recovery": {
        "socketExists": True,
        "tokenExists": True,
        "stateExists": True,
        "statePhase": "recovering",
    },
}


def _ensure_daemon_worker(
    runtime_home: str,
    workspace: str,
    start_barrier: Any,
    results: Any,
    fault_stage: str | None = None,
    fault_marker: str | None = None,
) -> None:
    os.environ["AGENT_CORE_HOME"] = runtime_home
    os.environ["AGENT_CORE_DAEMON_INSTANCE_LOCK_TIMEOUT_MS"] = "15000"
    if fault_stage:
        os.environ["AGENT_CORE_TEST_FAULTS"] = "daemon-boot"
        os.environ["AGENT_CORE_TEST_DAEMON_BOOT_FAULT_STAGE"] = fault_stage
    if fault_marker:
        os.environ["P02_FAULT_MARKER"] = fault_marker
    core.configureRuntimeHome(runtime_home)
    try:
        start_barrier.wait(timeout=20)

        async def ensure() -> dict[str, Any]:
            client = await core.ensureTaskSupervisorDaemon(
                {
                    "instanceId": P02_INSTANCE,
                    "cwd": workspace,
                    "startupTimeoutSeconds": 15,
                }
            )
            try:
                state = await client["getStatus"]()
                return _state_identity(state)
            finally:
                await client["close"]()

        results.put({"phase": "ready", "ok": True, "state": asyncio.run(ensure())})
    except BaseException as exc:
        results.put(
            {
                "phase": "ready",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def _reattach_daemon_worker(
    runtime_home: str,
    workspace: str,
    start_barrier: Any,
    crash_released: Any,
    recovery_barrier: Any,
    results: Any,
) -> None:
    os.environ["AGENT_CORE_HOME"] = runtime_home
    os.environ["AGENT_CORE_DAEMON_INSTANCE_LOCK_TIMEOUT_MS"] = "15000"
    core.configureRuntimeHome(runtime_home)
    failure_phase = "attached"

    async def attach() -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal failure_phase
        start_barrier.wait(timeout=20)
        first = await core.ensureTaskSupervisorDaemon(
            {
                "instanceId": P02_INSTANCE,
                "cwd": workspace,
                "startupTimeoutSeconds": 15,
            }
        )
        try:
            first_state = _state_identity(await first["getStatus"]())
            results.put({"phase": "attached", "ok": True, "state": first_state})
            failure_phase = "recovered"
            if not crash_released.wait(timeout=20):
                raise TimeoutError("P02 ready-daemon crash was not released")
            try:
                os.waitpid(first_state["pid"], 0)
            except ChildProcessError:
                pass
        finally:
            await first["close"]()

        recovery_barrier.wait(timeout=20)
        second = await core.ensureTaskSupervisorDaemon(
            {
                "instanceId": P02_INSTANCE,
                "cwd": workspace,
                "startupTimeoutSeconds": 15,
            }
        )
        try:
            second_state = _state_identity(await second["getStatus"]())
            return first_state, second_state
        finally:
            await second["close"]()

    try:
        first_state, second_state = asyncio.run(attach())
        results.put(
            {
                "phase": "recovered",
                "ok": True,
                "oldState": first_state,
                "state": second_state,
            }
        )
    except BaseException as exc:
        results.put(
            {
                "phase": failure_phase,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def _state_identity(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise AssertionError(f"P02 daemon state missing: {state!r}")
    return {
        "pid": state.get("pid"),
        "phase": state.get("phase"),
        "epoch": state.get("epoch"),
        "protocolVersion": state.get("protocolVersion"),
        "authTokenId": state.get("authTokenId"),
        "socketPath": state.get("socketPath"),
    }


def _collect(results: Any, count: int, timeout: float = 40) -> list[dict[str, Any]]:
    collected = []
    deadline = time.monotonic() + timeout
    while len(collected) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"P02 received {len(collected)}/{count} worker results")
        try:
            collected.append(results.get(timeout=remaining))
        except queue.Empty as exc:
            raise TimeoutError(
                f"P02 received {len(collected)}/{count} worker results"
            ) from exc
    return collected


def _wait_dead(pid: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while _is_alive(pid):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"P02 daemon pid {pid} did not exit")
        time.sleep(0.02)


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class P02ConcurrentDaemonBootstrapTests(AgentCoreTestCase):
    def test_two_clients_converge_after_each_bootstrap_crash_boundary(self):
        context = multiprocessing.get_context("spawn")
        for fault_stage, expected_boundary in _FAULT_STAGES.items():
            with self.subTest(fault_stage=fault_stage):
                root, home, workspace, previous = self.temp_context()
                workers: list[multiprocessing.Process] = []
                try:
                    core.configureRuntimeHome(str(home))
                    marker = Path(root.name, f"p02-{fault_stage}.marker")
                    barrier = context.Barrier(2)
                    results = context.Queue()
                    workers = [
                        context.Process(
                            target=_ensure_daemon_worker,
                            args=(
                                str(home),
                                str(workspace),
                                barrier,
                                results,
                                fault_stage,
                                str(marker),
                            ),
                        )
                        for _ in range(2)
                    ]
                    for worker in workers:
                        worker.start()
                    messages = _collect(results, 2)
                    for worker in workers:
                        worker.join(20)
                    self.assertTrue(all(worker.exitcode == 0 for worker in workers))
                    self.assertTrue(all(message["ok"] for message in messages), messages)
                    observed_boundary = json.loads(marker.read_text(encoding="utf-8"))
                    self.assertEqual(observed_boundary.pop("stage"), fault_stage)
                    self.assertEqual(observed_boundary, expected_boundary)
                    self._assert_single_authenticated_winner(
                        home,
                        [message["state"] for message in messages],
                    )
                finally:
                    self._stop_workers(workers)
                    run(core.stopTaskSupervisorDaemon({"instanceId": P02_INSTANCE}))
                    self.restore_home(root, previous)

    def test_ready_leader_crash_reauthenticates_both_attached_followers(self):
        root, home, workspace, previous = self.temp_context()
        context = multiprocessing.get_context("spawn")
        workers: list[multiprocessing.Process] = []
        try:
            core.configureRuntimeHome(str(home))
            start_barrier = context.Barrier(2)
            recovery_barrier = context.Barrier(2)
            crash_released = context.Event()
            results = context.Queue()
            workers = [
                context.Process(
                    target=_reattach_daemon_worker,
                    args=(
                        str(home),
                        str(workspace),
                        start_barrier,
                        crash_released,
                        recovery_barrier,
                        results,
                    ),
                )
                for _ in range(2)
            ]
            for worker in workers:
                worker.start()
            attached = _collect(results, 2)
            self.assertTrue(all(message["phase"] == "attached" for message in attached))
            self.assertTrue(all(message["ok"] for message in attached), attached)
            old_states = [message["state"] for message in attached]
            self._assert_single_authenticated_winner(home, old_states)
            old_pid = old_states[0]["pid"]
            os.kill(old_pid, signal.SIGKILL)
            crash_released.set()
            _wait_dead(old_pid)

            recovered = _collect(results, 2)
            for worker in workers:
                worker.join(20)
            self.assertTrue(all(worker.exitcode == 0 for worker in workers))
            self.assertTrue(
                all(message["phase"] == "recovered" for message in recovered)
            )
            self.assertTrue(all(message["ok"] for message in recovered), recovered)
            new_states = [message["state"] for message in recovered]
            self._assert_single_authenticated_winner(home, new_states)
            self.assertNotEqual(new_states[0]["pid"], old_states[0]["pid"])
            self.assertNotEqual(new_states[0]["epoch"], old_states[0]["epoch"])
            self.assertNotEqual(
                new_states[0]["authTokenId"], old_states[0]["authTokenId"]
            )
            events = run(core.loadDaemonEvents(P02_INSTANCE))
            self.assertTrue(
                any(
                    event.get("event") == "daemon.recovered"
                    and event.get("data", {}).get("epoch") == new_states[0]["epoch"]
                    for event in events
                )
            )
        finally:
            crash_released.set()
            self._stop_workers(workers)
            run(core.stopTaskSupervisorDaemon({"instanceId": P02_INSTANCE}))
            self.restore_home(root, previous)

    def _assert_single_authenticated_winner(
        self,
        home: Path,
        states: list[dict[str, Any]],
    ) -> None:
        identities = {
            (
                state["pid"],
                state["epoch"],
                state["authTokenId"],
                state["socketPath"],
            )
            for state in states
        }
        self.assertEqual(len(identities), 1, states)
        state = states[0]
        self.assertEqual(state["phase"], "running")
        self.assertTrue(_is_alive(state["pid"]))
        run(verifyPrivateDaemonRuntime(P02_INSTANCE))
        socket_identity = verifyOwnedDaemonSocket(state["socketPath"])
        self.assertIsNotNone(socket_identity)
        auth = run(readDaemonAuthMaterial(P02_INSTANCE, state))
        self.assertEqual(daemonTokenId(auth["token"]), state["authTokenId"])
        ledger = Path(home, "daemon", "instances", P02_INSTANCE, "spawn-ledger.jsonl")
        spawned = [json.loads(line)["pid"] for line in ledger.read_text().splitlines()]
        live_spawned = {pid for pid in spawned if _is_alive(pid)}
        self.assertEqual(live_spawned, {state["pid"]})
        client = run(core.connectTaskSupervisorDaemonClient({"instanceId": P02_INSTANCE}))
        self.assertIsNotNone(client)
        try:
            connected = run(client["getStatus"]())
            self.assertEqual(_state_identity(connected), state)
        finally:
            run(client["close"]())

    def _stop_workers(self, workers: list[multiprocessing.Process]) -> None:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(5)


if __name__ == "__main__":
    import unittest

    unittest.main()
