from __future__ import annotations

import unittest

import agent_core._facade as core
from agent_core.tasks.supervisor_parts.monitor_watch import tick_daemon_monitors
from tests.support.agent_core_case import AgentCoreTestCase, run

SESSION_ID = "70000000-0000-4000-8000-000000000001"


def _completed_task_with_monitor(task_id: str) -> dict:
    run(
        core.write_task_meta(
            task_id,
            {
                "type": "background_command",
                "taskId": task_id,
                "id": task_id,
                "sessionId": SESSION_ID,
                "status": "completed",
                "exitCode": 0,
                "startedAt": "2026-01-01T00:00:00Z",
                "finishedAt": "2026-01-01T00:01:00Z",
                "outputFile": core.task_output_path(task_id),
            },
        )
    )
    monitor = core.create_monitor_record(sessionId=SESSION_ID, taskId=task_id, goal="watch the build")
    run(core.save_monitor(monitor))
    return monitor


class DaemonMonitorWatchTests(AgentCoreTestCase):
    def test_tick_commits_terminal_transition_with_transition_id_and_event(self):
        """Bug regression: the monitor engine only ran with the daemon OFF
        (default-on), so no tick ever fired in production, while a weaker
        terminalizer rewrote monitors with no revision/transitionId/event."""
        root, home, workspace, previous = self.temp_context()
        try:
            monitor = _completed_task_with_monitor("task-watched")
            events: list[tuple] = []

            async def emit(event, data, session_id=None):
                events.append((event, data, session_id))

            transitions = run(tick_daemon_monitors(emit))
            self.assertEqual(transitions, 1)
            committed = run(core.load_monitor(monitor["id"]))
            self.assertEqual(committed["status"], "completed")
            self.assertGreaterEqual(committed["revision"], 1)
            self.assertTrue(committed["lastTransitionId"].startswith(f"monitor:{monitor['id']}:"))
            status_events = [entry for entry in events if entry[0] == "monitor.status_changed"]
            self.assertEqual(len(status_events), 1)
            self.assertEqual(status_events[0][1]["status"], "completed")
            self.assertEqual(status_events[0][1]["previous_status"], "active")
            self.assertIn("transition_id", status_events[0][1])
            self.assertEqual(status_events[0][2], SESSION_ID)

            # A second tick observes a terminal monitor and must not re-fire.
            self.assertEqual(run(tick_daemon_monitors(emit)), 0)
        finally:
            self.restore_home(root, previous)

    def test_supervisor_construction_ticks_monitors(self):
        """The production path: monitors transition under the daemon's
        supervisor, not only when the daemon is disabled."""
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            monitor = _completed_task_with_monitor("task-supervised")
            events: list[dict] = []

            async def on_event(event):
                events.append(event)

            supervisor = run(core.create_task_supervisor({"cwd": str(workspace), "onEvent": on_event}))
            committed = run(core.load_monitor(monitor["id"]))
            self.assertEqual(committed["status"], "completed")
            self.assertTrue(
                any(entry.get("event") == "monitor.status_changed" for entry in events)
            )
        finally:
            if supervisor is not None:
                run(supervisor["close"]())
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
