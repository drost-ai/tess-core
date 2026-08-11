from __future__ import annotations

import asyncio
import os
from pathlib import Path

import agent_core._facade as core
from agent_core.runtime.lifecycle_barrier import (
    LifecycleCloseTimeoutError,
    LifecycleClosedError,
    construct_with_rollback,
    create_lifecycle_barrier,
)
from agent_core.runtime.paths import (
    get_active_runtime_paths,
    resolve_runtime_paths,
    run_with_runtime_paths,
)
from tests.support.agent_core_case import AgentCoreTestCase, run


class RuntimePathsLifecycleTests(AgentCoreTestCase):
    def test_async_scope_can_be_created_before_asyncio_run(self):
        paths = resolve_runtime_paths(candidate="/tmp/agent-core-runtime-paths-task")

        async def read_active_home():
            await asyncio.sleep(0)
            return get_active_runtime_paths().home

        pending = run_with_runtime_paths(paths, read_active_home)
        self.assertNotEqual(get_active_runtime_paths().home, paths.home)
        self.assertEqual(asyncio.run(pending), paths.home)
        self.assertNotEqual(get_active_runtime_paths().home, paths.home)

    def test_runtime_paths_resolve_one_frozen_complete_graph(self):
        paths = resolve_runtime_paths("/workspace", "relative-runtime")
        with self.assertRaises(Exception):
            paths.home = "/changed"  # type: ignore[misc]
        self.assertEqual(paths.home, os.path.abspath("/workspace/relative-runtime"))
        self.assertEqual(paths.sessionsDir, os.path.join(paths.home, "sessions"))
        self.assertEqual(paths.taskRecordsDir, os.path.join(paths.home, "task-records"))
        self.assertEqual(paths.repositoryTrustDir, os.path.join(paths.home, "trust", "repositories"))
        self.assertEqual(paths.daemon_logs_dir, os.path.join(paths.home, "daemon", "logs"))
        self.assertEqual(paths.privateSecretsDir, os.path.join(paths.home, "private-secrets"))

    def test_immutable_runtime_scope_rejects_path_switch_across_awaits(self):
        root, home, workspace, previous = self.temp_context()
        home_a = home.parent / "path-a"
        home_b = home.parent / "path-b"
        try:
            paths_a = resolve_runtime_paths(candidate=str(home_a))

            async def scenario():
                async with run_with_runtime_paths(paths_a):
                    self.assertIs(get_active_runtime_paths(), paths_a)
                    self.assertEqual(core.get_state_root(), str(home_a))
                    await asyncio.sleep(0)
                    with self.assertRaisesRegex(RuntimeError, "scope is immutable"):
                        core.configure_runtime_home(str(home_b))
                    self.assertEqual(core.get_state_root(), str(home_a))

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_base_layout_creates_runtime_owned_directories_privately(self):
        root, home, workspace, previous = self.temp_context()
        paths = resolve_runtime_paths(candidate=str(home.parent / "layout"))
        try:
            async def scenario():
                async with run_with_runtime_paths(paths):
                    await core.ensure_base_layout(str(workspace), paths)

            run(scenario())
            for directory in (
                paths.home,
                paths.sessionsDir,
                paths.taskRecordsDir,
                paths.repositoryTrustDir,
                paths.daemonDir,
                paths.locksDir,
                paths.privateSecretsDir,
            ):
                self.assertTrue(Path(directory).is_dir())
                if os.name != "nt":
                    self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        finally:
            self.restore_home(root, previous)

    def test_close_rejects_new_work_and_drains_accepted_work(self):
        async def scenario():
            barrier = create_lifecycle_barrier()
            gate = asyncio.Event()
            trace: list[str] = []

            async def slow():
                trace.append("started")
                await gate.wait()
                trace.append("finished")

            running = barrier.run("slow operation", slow)
            await asyncio.sleep(0)
            closing = barrier.close(cleanup=lambda: trace.append("cleanup"))
            self.assertEqual(barrier.state, "closing")
            with self.assertRaises(LifecycleClosedError):
                barrier.run("late operation", lambda: None)
            await asyncio.sleep(0.02)
            self.assertFalse(closing.done())
            gate.set()
            await asyncio.gather(running, closing)
            self.assertEqual(trace, ["started", "cleanup", "finished"])
            self.assertEqual(barrier.state, "closed")
            self.assertEqual(barrier.inFlightCount, 0)

        run(scenario())

    def test_close_is_idempotent_escalates_and_timeout_stays_closing(self):
        async def scenario():
            barrier = create_lifecycle_barrier()
            gate = asyncio.Event()
            escalations: list[int] = []
            barrier.run("blocked finalizer", gate.wait)
            first = barrier.close(
                escalation_after_ms=10,
                on_escalation=escalations.append,
                fail_after_ms=30,
            )
            self.assertIs(first, barrier.close())
            with self.assertRaises(LifecycleCloseTimeoutError) as error:
                await first
            self.assertEqual(error.exception.pending, 1)
            self.assertEqual(escalations, [1])
            self.assertEqual(barrier.state, "closing")
            gate.set()
            await asyncio.sleep(0)

        run(scenario())

    def test_construction_rollback_runs_reverse_and_retains_cleanup_errors(self):
        async def scenario():
            trace: list[str] = []
            failure = RuntimeError("boot failed")

            async def build(cleanup):
                cleanup.defer("first", lambda: trace.append("first"))
                cleanup.defer("second", lambda: trace.append("second"))

                def faulty():
                    trace.append("faulty")
                    raise RuntimeError("cleanup failed")

                cleanup.defer("faulty", faulty)
                raise failure

            with self.assertRaises(RuntimeError) as error:
                await construct_with_rollback(build)
            self.assertIs(error.exception, failure)
            self.assertEqual(len(error.exception.cleanupErrors), 1)
            self.assertEqual(trace, ["faulty", "second", "first"])

        run(scenario())
