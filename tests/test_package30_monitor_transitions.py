from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from tests.support.tess_core_case import TessCoreTestCase, core, run


class Package30MonitorTransitionTests(TessCoreTestCase):
    def _fixture(self, home: Path, workspace: Path, status: str, suffix: str = "1"):
        config = run(core.resolve_config({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
        session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
        stamp = "2026-07-12T22:01:39.608Z"
        monitor = {
            "id": f"monitor-{suffix}",
            "sessionId": session["id"],
            "taskId": f"task-{suffix}",
            "createdAt": "2026-07-12T22:00:58.262Z",
            "updatedAt": stamp,
            "status": status,
            "goal": "Watch the test task",
            "checkIntervalMs": 20_000,
            "heartbeatIntervalMs": 60_000,
        }
        if status in {"completed", "failed"}:
            monitor["completedAt"] = stamp
        if status == "failed":
            monitor["failureReason"] = "task failed"
        if status == "stopped":
            monitor["stoppedAt"] = stamp
        run(core.save_monitor(monitor))
        session["metadata"]["monitorIds"] = [monitor["id"]]
        events = []
        persisted = {"count": 0}
        context = {
            "session": session,
            "config": config,
            "interactive": False,
            "askQuestion": lambda *_: "",
            "outputJson": lambda event, data: events.append((event, data)),
            "cwd": str(workspace),
            "persist_session": lambda: persisted.__setitem__("count", persisted["count"] + 1),
        }
        tool = next(tool for tool in core.create_builtin_tools() if tool.name == "TaskMonitorStop")
        return config, session, monitor, events, persisted, context, tool

    def test_active_and_muted_stop_once_without_failure_and_preserve_underlying_task(self):
        root, home, workspace, previous = self.temp_context()
        try:
            for index, status in enumerate(("active", "muted"), 1):
                with self.subTest(status=status):
                    _config, _session, monitor, events, _persisted, context, tool = self._fixture(home, workspace, status, str(index))
                    task = {"type": "background_command", "taskId": monitor["taskId"], "sessionId": monitor["sessionId"], "status": "running", "pid": os.getpid(), "startedAt": "2026-07-12T22:00:00.000Z"}
                    run(core.write_task_meta(monitor["taskId"], task))
                    task_path = Path(core.task_meta_path(monitor["taskId"]))
                    task_before = task_path.read_bytes()
                    first = json.loads(run(tool.handler({"monitor_id": monitor["id"]}, context))["content"])
                    self.assertEqual((first["previous_status"], first["status"], first["changed"]), (status, "stopped", True))
                    self.assertEqual([event for event, _ in events], ["monitor.status_changed"])
                    self.assertFalse(any(event == "task.failed" for event, _ in events))
                    self.assertEqual(task_path.read_bytes(), task_before)

                    stopped_path = Path(core.monitor_path(monitor["id"]))
                    stopped_bytes = stopped_path.read_bytes()
                    stopped_mtime = stopped_path.stat().st_mtime_ns
                    events.clear()
                    second = json.loads(run(tool.handler({"monitor_id": monitor["id"]}, context))["content"])
                    self.assertFalse(second["changed"])
                    self.assertEqual(second["status"], "stopped")
                    self.assertEqual(stopped_path.read_bytes(), stopped_bytes)
                    self.assertEqual(stopped_path.stat().st_mtime_ns, stopped_mtime)
                    self.assertEqual(events, [])
        finally:
            self.restore_home(root, previous)

    def test_completed_failed_and_stopped_are_byte_identical_event_free_noops(self):
        root, home, workspace, previous = self.temp_context()
        try:
            for index, status in enumerate(("completed", "failed", "stopped"), 10):
                with self.subTest(status=status):
                    _config, _session, monitor, events, persisted, context, tool = self._fixture(home, workspace, status, str(index))
                    path = Path(core.monitor_path(monitor["id"]))
                    before = (path.read_bytes(), path.stat().st_mtime_ns)
                    payload = json.loads(run(tool.handler({"monitor_id": monitor["id"]}, context))["content"])
                    self.assertEqual((payload["status"], payload["previous_status"], payload["changed"]), (status, status, False))
                    self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
                    self.assertEqual(events, [])
                    self.assertEqual(persisted["count"], 0)
        finally:
            self.restore_home(root, previous)

    def test_slash_stop_and_resume_share_terminal_aware_policy(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session, monitor, _events, _persisted, _context, _tool = self._fixture(home, workspace, "completed", "20")
            path = Path(core.monitor_path(monitor["id"]))
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            calls = {"persist": 0, "refresh": 0}
            infos = []
            hooks = {
                "info": lambda title, message: infos.append((title, message)),
                "error": lambda message: self.fail(message),
                "persist": lambda: calls.__setitem__("persist", calls["persist"] + 1),
                "refresh": lambda: calls.__setitem__("refresh", calls["refresh"] + 1),
            }
            run(core.handle_slash_command({"command": f"/monitor stop {monitor['id']}", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
            self.assertEqual(calls, {"persist": 0, "refresh": 0})
            self.assertIn("already completed", infos[-1][1])

            run(core.handle_slash_command({"command": f"/monitor resume {monitor['id']}", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
            self.assertEqual(calls, {"persist": 0, "refresh": 0})
            self.assertIn("cannot be resumed", infos[-1][1])
        finally:
            self.restore_home(root, previous)

    def test_active_stop_projects_exactly_one_neutral_task_stopped_event(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session, monitor, tool_events, _persisted, context, tool = self._fixture(home, workspace, "active", "30")

            async def scenario():
                projected = []
                runtime = await core.create_task_runtime({"session": session, "config": config, "emit": lambda event, data: projected.append((event, data)), "tickIntervalMs": 30})
                try:
                    for _ in range(100):
                        if any(event == "task.registered" for event, _ in projected):
                            break
                        await asyncio.sleep(0.02)
                    await tool.handler({"monitor_id": monitor["id"]}, context)
                    for _ in range(100):
                        if any(event == "task.stopped" for event, _ in projected):
                            break
                        await asyncio.sleep(0.02)
                    await asyncio.sleep(0.12)
                    return projected, tool_events
                finally:
                    await runtime["close"]()

            projected, tool_events = run(scenario())
            self.assertEqual(sum(event == "task.stopped" for event, _ in projected), 1)
            self.assertEqual(sum(event == "task.failed" for event, _ in projected), 0)
            self.assertEqual([event for event, _ in tool_events], ["monitor.status_changed"])
        finally:
            self.restore_home(root, previous)

    def test_session_ownership_mismatch_cannot_mutate_monitor(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, _session, monitor, events, _persisted, context, tool = self._fixture(home, workspace, "active", "40")
            path = Path(core.monitor_path(monitor["id"]))
            before = path.read_bytes()
            context["session"] = {**context["session"], "id": "different-session"}
            with self.assertRaises(core.TaskOwnershipError) as error:
                run(tool.handler({"monitor_id": monitor["id"]}, context))
            self.assertEqual(error.exception.code, "task.not_owned")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(events, [])
        finally:
            self.restore_home(root, previous)
