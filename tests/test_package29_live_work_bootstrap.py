from __future__ import annotations

import asyncio
import importlib
import os
import time
from pathlib import Path

from tests.support.agent_core_case import AgentCoreTestCase, core, run
from agent_core._utils import now_iso


live_work = importlib.import_module("agent_core.tasks.supervisor_parts.live_work")
collaboration_module = importlib.import_module("agent_core.collaboration")
collaboration_runtime_module = importlib.import_module("agent_core.collaboration.runtime")
monitors_module = importlib.import_module("agent_core.tasks.monitors")

_OLD = "2026-01-01T00:00:00Z"
_DEAD_PID = 99_999_999


class Package29LiveWorkTests(AgentCoreTestCase):
    def _session(self, workspace: Path, session_id: str, metadata: dict | None = None) -> dict:
        session = core.createEmptySession(
            cwd=str(workspace), provider="openai", model="test-model", systemPrompt="test",
            permissionMode="default", sessionId=session_id, originalCwd=str(workspace),
        )
        session["messages"] = [{"role": "user", "content": "hello"}]
        session["metadata"].update(metadata or {})
        return session

    def _task(self, task_id: str, session_id: str, **overrides) -> dict:
        return {
            "type": "background_command", "taskId": task_id, "sessionId": session_id,
            "status": "running", "pid": os.getpid(), "startedAt": _OLD, "updatedAt": _OLD,
            "command": "sleep 600", **overrides,
        }

    def test_process_liveness_terminal_filtering_and_one_time_reconciliation(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "61111111-1111-4111-8111-111111111111", {"taskIds": ["live", "done", "dead"]})
            run(core.writeTaskMeta("live", self._task("live", session["id"])))
            run(core.writeTaskMeta("done", self._task("done", session["id"], status="completed")))
            run(core.writeTaskMeta("dead", self._task("dead", session["id"], pid=_DEAD_PID)))

            verdict = run(live_work._resolve_session_live_work(session))
            self.assertTrue(verdict["live"])
            self.assertEqual(verdict["families"], ["tasks"])
            self.assertIn("task:dead", verdict["reconciled"])
            self.assertTrue(any(value.startswith("task:done:completed") for value in verdict["stale"]))
            dead = run(core.readTaskMeta("dead"))
            self.assertEqual(dead["status"], "terminated")
            self.assertNotIn("error", dead)
            raw = Path(core.taskMetaPath("dead")).read_bytes()
            mtime = Path(core.taskMetaPath("dead")).stat().st_mtime_ns
            second = run(live_work._resolve_session_live_work(session))
            self.assertNotIn("task:dead", second["reconciled"])
            self.assertEqual(Path(core.taskMetaPath("dead")).read_bytes(), raw)
            self.assertEqual(Path(core.taskMetaPath("dead")).stat().st_mtime_ns, mtime)
        finally:
            self.restore_home(root, previous)

    def test_dead_subagent_fails_but_recent_dead_process_stays_in_grace(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(
                workspace,
                "62222222-2222-4222-8222-222222222222",
                {"taskIds": ["sub-dead", "recent"], "childAgentIds": ["agent-dead"]},
            )
            run(core.writeTaskMeta("sub-dead", self._task("sub-dead", "child", type="subagent", parent_session_id=session["id"], agent_id="agent-dead", pid=_DEAD_PID)))
            run(core.saveChildAgent({"agentId": "agent-dead", "sessionId": "child", "parentSessionId": session["id"], "status": "backgrounded", "pid": _DEAD_PID, "startedAt": _OLD}))
            run(core.writeTaskMeta("recent", self._task("recent", session["id"], pid=_DEAD_PID, startedAt=now_iso())))

            verdict = run(live_work._resolve_session_live_work(session))
            self.assertTrue(verdict["live"])
            self.assertEqual(verdict["families"], ["tasks"])
            sub = run(core.readTaskMeta("sub-dead"))
            child = run(core.loadChildAgent("agent-dead"))
            self.assertEqual((sub["status"], sub["completionReason"]), ("failed", "worker_lost"))
            self.assertEqual((child["status"], child["completionReason"]), ("failed", "worker_lost"))
            self.assertEqual(run(core.readTaskMeta("recent"))["status"], "running")
        finally:
            self.restore_home(root, previous)

    def test_mixed_family_verdict_filters_terminal_invalid_and_remote_records(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(
                workspace,
                "63333333-3333-4333-8333-333333333333",
                {"taskIds": ["task-live"], "childAgentIds": ["agent-live"], "monitorIds": ["monitor-live"], "remoteSessionId": "remote-live", "bridgeId": "bridge-live"},
            )
            run(core.writeTaskMeta("task-live", self._task("task-live", session["id"])))
            run(core.saveChildAgent({"agentId": "agent-live", "sessionId": "child-live", "parentSessionId": session["id"], "status": "running", "startedAt": _OLD}))
            run(core.writeTaskMeta("monitored", self._task("monitored", session["id"])))
            run(core.saveMonitor({"id": "monitor-live", "sessionId": session["id"], "taskId": "monitored", "status": "muted", "createdAt": _OLD, "updatedAt": _OLD, "goal": "watch"}))
            health = {"taskId": "health-live", "sessionId": session["id"], "status": "unhealthy", "mode": "command", "command": "true"}
            invalid_health = {"taskId": "health-invalid", "sessionId": session["id"], "status": "active", "mode": "command"}
            run(core.saveRemoteSession({"id": "remote-live", "status": "active", "bridgeId": "bridge-live"}))
            run(core.saveBridge({"id": "bridge-live", "status": "active"}))

            verdict = run(live_work._resolve_session_live_work(session, {"healthChecks": [health, invalid_health]}))
            self.assertEqual(verdict["families"], ["tasks", "subagents", "monitors", "health_checks", "remote"])
            self.assertTrue(any(value.startswith("health_check:health-invalid") for value in verdict["stale"]))

            run(core.writeTaskMeta("monitored", self._task("monitored", session["id"], status="completed")))
            run(core.saveRemoteSession({"id": "remote-live", "status": "closed", "bridgeId": "bridge-live"}))
            terminal_context = {
                "healthChecks": [{**health, "status": "stopped"}],
                "childAgents": [{"agentId": "agent-live", "sessionId": "child-live", "parentSessionId": session["id"], "status": "completed"}],
            }
            terminal = run(live_work._resolve_session_live_work(session, terminal_context))
            self.assertEqual(terminal["families"], ["tasks"])
            self.assertEqual(run(core.loadMonitor("monitor-live"))["status"], "completed")

            opaque = self._session(workspace, "64444444-4444-4444-8444-444444444444", {"remoteSessionId": "opaque"})
            self.assertEqual(run(live_work._resolve_session_live_work(opaque))["families"], ["remote"])
        finally:
            self.restore_home(root, previous)

    def test_verdict_cache_hits_and_explicit_invalidation(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = self._session(workspace, "65555555-5555-4555-8555-555555555555", {"taskIds": ["cached"]})
            run(core.writeTaskMeta("cached", self._task("cached", session["id"])))
            cache = live_work._create_live_work_verdict_cache()
            self.assertTrue(run(cache["resolve"](session))["live"])
            self.assertTrue(run(cache["resolve"](session))["live"])
            self.assertEqual(cache["getStats"]()["hits"], 1)
            cache["invalidate"](session["id"])
            self.assertEqual(cache["getStats"]()["invalidations"], 1)
            run(core.writeTaskMeta("cached", self._task("cached", session["id"], status="completed")))
            self.assertFalse(run(cache["resolve"](session))["live"])
            self.assertEqual(cache["getStats"]()["misses"], 2)
        finally:
            self.restore_home(root, previous)


class Package29BootstrapNarrationTests(AgentCoreTestCase):
    def test_collaboration_bootstrap_is_silent_and_later_transition_narrates(self):
        root, _home, workspace, previous = self.temp_context()
        runtime = None
        original_send = collaboration_runtime_module.sendOpenAiRequest
        try:
            session = core.createEmptySession(cwd=str(workspace), provider="openai", model="test-model", systemPrompt="test", permissionMode="default", sessionId="66666666-6666-4666-8666-666666666666")
            session["messages"] = [{"role": "user", "content": "delegate"}]
            session["metadata"]["childAgentIds"] = ["agent-old"]
            run(core.saveSession(session))
            child = {"agentId": "agent-old", "sessionId": "child-old", "parentSessionId": session["id"], "name": "Researcher", "status": "backgrounded", "startedAt": _OLD, "taskId": "child-task"}
            run(core.saveChildAgent(child))
            calls = []

            async def fake_send(args):
                calls.append(args)
                return {"content": [{"type": "text", "text": "The researcher finished."}], "usage": {}}

            collaboration_runtime_module.sendOpenAiRequest = fake_send
            target = Path(core.sessionPath(session["id"]))
            before = (target.read_bytes(), target.stat().st_mtime_ns, len(session["messages"]))
            events = []
            runtime = run(core.createCollaborationRuntime({"session": session, "config": {"provider": "openai", "openaiToken": "test", "sessionPersistence": True}, "startTicker": False, "minAssistantUpdateSpacingMs": 0, "emit": lambda event, data: events.append((event, data))}))
            self.assertEqual(calls, [])
            self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns, len(session["messages"])), before)
            self.assertTrue(any(event == "subagent.status_changed" for event, _ in events))

            run(core.saveChildAgent({**child, "status": "completed", "completedAt": now_iso(), "lastUpdatedAt": now_iso(), "resultSummary": "done"}))
            run(runtime["refresh"]())
            self.assertEqual(len(calls), 1)
            self.assertTrue(any(event == "collaboration.assistant_update" for event, _ in events))
            self.assertEqual(session["messages"][-1]["content"], "The researcher finished.")
        finally:
            collaboration_runtime_module.sendOpenAiRequest = original_send
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_monitor_bootstrap_is_silent_and_later_output_narrates(self):
        root, _home, workspace, previous = self.temp_context()
        runtime = None
        original_send = monitors_module.sendOpenAiRequest
        try:
            session = core.createEmptySession(cwd=str(workspace), provider="openai", model="test-model", systemPrompt="test", permissionMode="default", sessionId="67777777-7777-4777-8777-777777777777")
            session["messages"] = [{"role": "user", "content": "monitor"}]
            run(core.saveSession(session))
            task_id = "monitor-bootstrap-task"
            output = Path(core.taskOutputPath(task_id))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("initial\n", encoding="utf-8")
            run(core.writeTaskMeta(task_id, {"type": "background_command", "taskId": task_id, "sessionId": session["id"], "status": "running", "startedAt": _OLD, "pid": os.getpid(), "outputFile": str(output)}))
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": task_id, "goal": "watch"})
            monitor["checkIntervalMs"] = 1
            monitor["createdAt"] = _OLD
            monitor["updatedAt"] = _OLD
            run(core.saveMonitor(monitor))
            calls = []

            async def fake_send(args):
                calls.append(args)
                return {"content": [{"type": "text", "text": "There is new output."}], "usage": {}}

            monitors_module.sendOpenAiRequest = fake_send
            target = Path(core.sessionPath(session["id"]))
            before = (target.read_bytes(), target.stat().st_mtime_ns, len(session["messages"]))
            events = []
            async def exercise_runtime():
                created = await core.createMonitorRuntime({"session": session, "config": {"provider": "openai", "openaiToken": "test", "sessionPersistence": True}, "emit": lambda event, data: events.append((event, data)), "tickIntervalMs": 10})
                self.assertEqual(calls, [])
                self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns, len(session["messages"])), before)
                with output.open("a", encoding="utf-8") as handle:
                    handle.write("x" * 256 + "\n")
                deadline = time.time() + 2
                while not calls and time.time() < deadline:
                    await asyncio.sleep(0.02)
                await created["close"]()

            run(exercise_runtime())
            self.assertEqual(len(calls), 1)
            self.assertTrue(any(event == "monitor.assistant_update" for event, _ in events))
            self.assertEqual(session["messages"][-1]["content"], "There is new output.")
        finally:
            monitors_module.sendOpenAiRequest = original_send
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)
