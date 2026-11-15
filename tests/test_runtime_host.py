"""start_runtime: one daemon per host process and runtime home. Real daemon
processes, because process ownership is the claim."""
from __future__ import annotations

import subprocess
import sys
import textwrap

from tests.support.tess_core_case import *

from tess_core import Runtime, RuntimeOwnershipError, start_runtime
from tess_core._utils import is_process_alive
from tess_core.daemon.lifecycle import _is_daemon_run_process
from tess_core.runtime import host as host_module
from tess_core.runtime.owner_lease import owner_lease_path, read_owner_lease
from tess_core.storage import load_daemon_state


def _daemon_pid(runtime: Runtime) -> int:
    state = run(load_daemon_state(runtime.instance_id))
    pid = (state or {}).get("pid")
    assert isinstance(pid, int), state
    return pid


def _wait(predicate, timeout=20.0, label=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {label}")


HOST_SCRIPT = textwrap.dedent(
    """
    import asyncio, json, os, sys
    from tess_core import RuntimeOwnershipError, start_runtime

    async def main():
        home, action = sys.argv[1], sys.argv[2]
        try:
            runtime = await start_runtime(home, cwd=os.getcwd(), startup_timeout_seconds=20)
        except RuntimeOwnershipError as error:
            print(json.dumps({"ok": False, "holder_pid": error.holder.get("pid"), "detail": str(error)}), flush=True)
            return
        status = runtime.status()
        print(json.dumps({"ok": True, "pid": os.getpid(), "daemon_pid": status["daemon"]["pid"], "instance": runtime.instance_id}), flush=True)
        if action == "hold":
            sys.stdin.readline()   # stay the owner until told to go
        await runtime.stop()
        print(json.dumps({"stopped": True}), flush=True)

    asyncio.run(main())
    """
)


def _host_process(home: str, workspace: str, action: str) -> subprocess.Popen[str]:
    env = {**os.environ, "TESS_CORE_HOME": str(home), "PYTHONPATH": str(Path(core.__file__).resolve().parents[1])}
    return subprocess.Popen(
        [sys.executable, "-c", HOST_SCRIPT, str(home), action],
        cwd=str(workspace), env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class RuntimeHostTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def test_start_owns_one_daemon_and_stop_ends_it(self):
        root, home, workspace, previous = self.temp_context()
        try:
            runtime = run(start_runtime(str(home), cwd=str(workspace), startup_timeout_seconds=20))
            try:
                self.assertIs(host_module.active_runtime(), runtime)
                pid = _daemon_pid(runtime)
                self.assertTrue(is_process_alive(pid))
                self.assertTrue(run(_is_daemon_run_process(pid, runtime.instance_id)))
                status = runtime.status()
                self.assertEqual(status["daemon"]["pid"], pid)
                self.assertEqual(status["daemon"]["ownerPid"], os.getpid())
                self.assertEqual(status["daemon"]["recoveries"], 0)
                self.assertTrue(status["daemon"]["connected"])
                self.assertEqual((status["sessions"]["open"], status["sessions"]["pending"]), ([], []))
                self.assertEqual(status["sessions"]["monitors"]["pending"], 0)
                lease = run(read_owner_lease(str(home)))
                self.assertEqual((lease["pid"], lease["instanceId"]), (os.getpid(), runtime.instance_id))
            finally:
                run(runtime.stop())
            self.assertFalse(is_process_alive(pid))
            self.assertTrue(runtime.stopped)
            self.assertIsNone(run(read_owner_lease(str(home))))
            self.assertIsNone(host_module.active_runtime())
            run(runtime.stop())  # idempotent
        finally:
            self.restore_home(root, previous)

    def test_second_runtime_in_the_same_process_is_refused_until_the_first_stops(self):
        root, home, workspace, previous = self.temp_context()
        try:
            first = run(start_runtime(str(home), cwd=str(workspace), startup_timeout_seconds=20))
            try:
                with self.assertRaises(RuntimeOwnershipError) as refused:
                    run(start_runtime(str(home), cwd=str(workspace)))
                self.assertIn("already running in this process", str(refused.exception))
                self.assertEqual(refused.exception.holder["pid"], os.getpid())
            finally:
                run(first.stop())
            second = run(start_runtime(str(home), cwd=str(workspace), startup_timeout_seconds=20))
            try:
                self.assertNotEqual(second.instance_id, first.instance_id)
                self.assertTrue(is_process_alive(_daemon_pid(second)))
            finally:
                run(second.stop())
        finally:
            self.restore_home(root, previous)

    def test_concurrent_starts_in_one_process_create_one_daemon(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def race():
                return await asyncio.gather(
                    *(start_runtime(str(home), cwd=str(workspace), startup_timeout_seconds=20) for _ in range(3)),
                    return_exceptions=True,
                )

            outcomes = run(race())
            runtimes = [item for item in outcomes if isinstance(item, Runtime)]
            refusals = [item for item in outcomes if isinstance(item, RuntimeOwnershipError)]
            try:
                self.assertEqual((len(runtimes), len(refusals)), (1, 2))
                instances = os.listdir(home / "daemon" / "instances")
                self.assertEqual(instances, [runtimes[0].instance_id])
            finally:
                for runtime in runtimes:
                    run(runtime.stop())
        finally:
            self.restore_home(root, previous)

    def test_second_host_process_on_the_same_home_is_refused_and_allowed_after_stop(self):
        root, home, workspace, previous = self.temp_context()
        holder = None
        try:
            holder = _host_process(home, workspace, "hold")
            started = json.loads(holder.stdout.readline())
            self.assertTrue(started["ok"], started)
            self.assertTrue(is_process_alive(started["daemon_pid"]))

            with self.assertRaises(RuntimeOwnershipError) as refused:
                run(start_runtime(str(home), cwd=str(workspace), startup_timeout_seconds=20))
            self.assertEqual(refused.exception.holder["pid"], started["pid"])
            self.assertIn(str(started["pid"]), str(refused.exception))
            # Being refused left the holder's daemon alone and started none of ours.
            self.assertTrue(is_process_alive(started["daemon_pid"]))
            self.assertEqual(os.listdir(home / "daemon" / "instances"), [started["instance"]])

            holder.stdin.write("go\n")
            holder.stdin.flush()
            self.assertTrue(json.loads(holder.stdout.readline())["stopped"])
            holder.wait(timeout=20)
            _wait(lambda: not is_process_alive(started["daemon_pid"]), label="holder's daemon exit")

            mine = run(start_runtime(str(home), cwd=str(workspace), startup_timeout_seconds=20))
            try:
                self.assertTrue(is_process_alive(_daemon_pid(mine)))
            finally:
                run(mine.stop())
        finally:
            if holder is not None and holder.poll() is None:
                holder.kill()
            self.restore_home(root, previous)

    def test_lease_of_a_dead_host_is_reclaimed(self):
        root, home, workspace, previous = self.temp_context()
        try:
            from tess_core.process.identity import capture_process_birth_marker

            ghost = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                birth = run(capture_process_birth_marker(ghost.pid))
            finally:
                ghost.kill()
                ghost.wait()
            path = owner_lease_path(str(home))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            Path(path).write_text(json.dumps({
                "schemaVersion": 1, "pid": ghost.pid, "birth": birth, "instanceId": "ghost-instance",
                "startedAt": "2026-01-01T00:00:00Z", "reclaimedFrom": None,
            }))
            runtime = run(start_runtime(str(home), cwd=str(workspace), startup_timeout_seconds=20))
            try:
                lease = run(read_owner_lease(str(home)))
                self.assertEqual(lease["pid"], os.getpid())
                self.assertEqual(lease["reclaimedFrom"]["pid"], ghost.pid)
            finally:
                run(runtime.stop())
        finally:
            self.restore_home(root, previous)

    def test_separate_homes_are_isolated(self):
        root, home, workspace, previous = self.temp_context()
        other_home = Path(root.name) / "other-home"
        other_home.mkdir()
        other = None
        try:
            other = _host_process(other_home, workspace, "hold")
            started = json.loads(other.stdout.readline())
            self.assertTrue(started["ok"], started)
            runtime = run(start_runtime(str(home), cwd=str(workspace), startup_timeout_seconds=20))
            try:
                self.assertNotEqual(_daemon_pid(runtime), started["daemon_pid"])
                self.assertEqual(os.listdir(home / "daemon" / "instances"), [runtime.instance_id])
                self.assertEqual(os.listdir(other_home / "daemon" / "instances"), [started["instance"]])
            finally:
                run(runtime.stop())
            self.assertTrue(is_process_alive(started["daemon_pid"]))  # ours stopping did not touch theirs
            other.stdin.write("go\n")
            other.stdin.flush()
            other.wait(timeout=20)
        finally:
            if other is not None and other.poll() is None:
                other.kill()
            self.restore_home(root, previous)

    def test_lost_daemon_is_replaced_once_by_the_runtime_watchdog(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ["TESS_CORE_DAEMON_HEARTBEAT_MS"] = "300"
            events = []

            class Listener:
                def on_runtime_event(self, event, data):
                    events.append(event)

            runtime = run(start_runtime(str(home), cwd=str(workspace), listener=Listener(), startup_timeout_seconds=20))
            try:
                first_pid = _daemon_pid(runtime)
                os.kill(first_pid, 9)
                _wait(lambda: runtime.status()["daemon"]["recoveries"] == 1, label="one recovery")
                second_pid = runtime.status()["daemon"]["pid"]
                self.assertNotEqual(second_pid, first_pid)
                self.assertTrue(is_process_alive(second_pid))
                self.assertTrue(run(_is_daemon_run_process(second_pid, runtime.instance_id)))
                time.sleep(1.0)  # several more heartbeats: still exactly one recovery, one instance
                self.assertEqual(runtime.status()["daemon"]["recoveries"], 1)
                self.assertEqual(os.listdir(home / "daemon" / "instances"), [runtime.instance_id])
                self.assertEqual(events.count("daemon.restarted"), 1)
                self.assertTrue(runtime.status()["daemon"]["connected"])
            finally:
                run(runtime.stop())
            self.assertFalse(is_process_alive(second_pid))
        finally:
            os.environ.pop("TESS_CORE_DAEMON_HEARTBEAT_MS", None)
            self.restore_home(root, previous)

    def test_daemon_disabled_runtime_has_no_daemon_and_takes_no_lease(self):
        root, home, workspace, previous = self.temp_context()
        try:
            runtime = run(start_runtime(str(home), cwd=str(workspace), daemon_enabled=False))
            try:
                self.assertIsNone(runtime.status()["daemon"])
                self.assertIsNone(runtime.daemon)
                self.assertFalse(os.path.exists(owner_lease_path(str(home))))
                self.assertFalse(os.path.isdir(home / "daemon" / "instances"))
            finally:
                run(runtime.stop())
        finally:
            self.restore_home(root, previous)

    def test_daemon_less_runtimes_coexist_in_one_process_and_never_block_a_host(self):
        """One daemon-owning runtime per process; a daemon-less runtime (a
        memory distiller running turns in a worker thread) owns nothing and
        any number may run beside it and beside the host's own runtime."""
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                first = await start_runtime(str(home), cwd=str(workspace), daemon_enabled=False)
                second = await start_runtime(str(home), cwd=str(workspace), daemon_enabled=False)
                host = await start_runtime(str(home), cwd=str(workspace), startup_timeout_seconds=20)
                try:
                    self.assertIs(host_module.active_runtime(), host)
                    self.assertNotEqual(first.instance_id, second.instance_id)
                    for companion in (first, second):
                        self.assertIsNone(companion.status()["daemon"])
                        handle = await companion.sessions.open()
                        self.assertTrue(handle.is_open)
                    with self.assertRaises(RuntimeOwnershipError):
                        await start_runtime(str(home), cwd=str(workspace))  # a second daemon owner: still refused
                    third = await start_runtime(str(home), cwd=str(workspace), daemon_enabled=False)
                    await third.stop()
                finally:
                    await first.stop()
                    await second.stop()
                    await host.stop()
                self.assertIsNone(host_module.active_runtime())

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_daemon_disabled_companion_does_not_block_a_host(self):
        """A daemon-less runtime in another process (a CLI reading the home)
        holds no lease, so the real host still starts."""
        root, home, workspace, previous = self.temp_context()
        try:
            companion = run(start_runtime(str(home), cwd=str(workspace), daemon_enabled=False))
            run(companion.stop())
            host = _host_process(home, workspace, "stop")
            out, err = host.communicate(timeout=60)
            started = json.loads(out.splitlines()[0])
            self.assertTrue(started["ok"], (started, err[-800:]))
        finally:
            self.restore_home(root, previous)
