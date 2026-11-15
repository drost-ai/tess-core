from __future__ import annotations

import os
import socket
import unittest
from unittest import mock

import tess_core.daemon as daemon_module
from tess_core.runtime._daemon_watch import check_daemon_once
from tess_core.storage import daemon_socket_path
from tess_core.storage.daemon import save_daemon_state
from tests.support.tess_core_case import TessCoreTestCase, run


class DaemonWatchdogDeadManTests(TessCoreTestCase):
    def test_live_pid_without_accepting_socket_is_declared_lost(self):
        """Dead-man regression: a daemon whose pid is alive (wedged listener,
        or a recycled pid behind a stale state file) must not read healthy —
        health requires the instance socket to accept."""
        root, home, workspace, previous = self.temp_context()
        try:
            events = []
            calls = []

            async def fake_ensure(args):
                calls.append(args)
                raise RuntimeError("unit test does not spawn a daemon")

            run(
                save_daemon_state(
                    "watchdog-wedged",
                    {"instanceId": "watchdog-wedged", "pid": os.getpid(), "status": "running"},
                )
            )
            with mock.patch.object(daemon_module, "ensure_task_supervisor_daemon", new=fake_ensure):
                outcome = run(
                    check_daemon_once(
                        {
                            "instanceId": "watchdog-wedged",
                            "cwd": str(workspace),
                            "emit": lambda event, data: events.append((event, data)),
                        }
                    )
                )
            self.assertEqual(outcome, "restart_failed")
            self.assertEqual(events[0][0], "daemon.lost")
            self.assertEqual(events[0][1]["reason"], "socket_inactive")
            self.assertEqual(len(calls), 1)
        finally:
            self.restore_home(root, previous)

    def test_live_pid_with_accepting_socket_is_healthy(self):
        root, home, workspace, previous = self.temp_context()
        listener = None
        try:
            socket_path = daemon_socket_path("watchdog-live")
            os.makedirs(os.path.dirname(socket_path), exist_ok=True)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(socket_path)
            listener.listen(1)
            run(
                save_daemon_state(
                    "watchdog-live",
                    {
                        "instanceId": "watchdog-live",
                        "pid": os.getpid(),
                        "status": "running",
                        "socketPath": socket_path,
                    },
                )
            )

            async def fail_ensure(args):
                raise AssertionError("healthy daemon must not be replaced")

            with mock.patch.object(daemon_module, "ensure_task_supervisor_daemon", new=fail_ensure):
                outcome = run(
                    check_daemon_once(
                        {
                            "instanceId": "watchdog-live",
                            "cwd": str(workspace),
                            "emit": lambda event, data: (_ for _ in ()).throw(AssertionError(event)),
                        }
                    )
                )
            self.assertEqual(outcome, "healthy")
        finally:
            if listener is not None:
                listener.close()
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
