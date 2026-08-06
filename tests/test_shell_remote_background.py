from __future__ import annotations

from tests.support.agent_core_case import *

import agent_core.shell_surface.tasks as shell_surface_tasks_module


class ShellRemoteBackgroundTests(AgentCoreTestCase):
    def test_shell_daemon_client_fallback_for_tasks_and_agents(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            run(core.saveTaskRecord({"id": "task-daemon-fallback", "sessionId": session["id"], "kind": "background_command", "status": "running", "title": "Daemon fallback task", "updatedAt": "2026-01-01T00:00:00Z"}))
            run(core.saveChildAgent({"agentId": "agent-daemon-fallback", "sessionId": "child-daemon-session", "parentSessionId": session["id"], "name": "Daemon worker", "provider": "openai", "model": "gpt-5.4", "mode": "auto", "runInBackground": True, "status": "running", "taskId": "task-daemon-fallback", "startedAt": "2026-01-01T00:00:00Z"}))
            session["metadata"]["childAgentIds"] = ["agent-daemon-fallback"]
            calls = []

            async def fake_connect():
                return {
                    "_daemonClient": True,
                    "readTaskOutput": lambda task_id, tail: calls.append(("read", task_id, tail)) or {"output": f"daemon output {task_id}", "truncated": True},
                    "stopTask": lambda task_id: calls.append(("stop", task_id)) or True,
                    "retryTask": lambda task_id: calls.append(("retry", task_id)) or {"taskId": "task-daemon-retry"},
                    "close": lambda: calls.append(("close",)),
                }

            with mock.patch.object(shell_surface_tasks_module, "connectTaskSupervisorDaemonClient", fake_connect):
                log = run(core.handleSlashCommand({"command": "/tasks log fallback", "session": session, "config": config, "hooks": {}}))
                stop = run(core.handleSlashCommand({"command": "/tasks stop fallback", "session": session, "config": config, "hooks": {}}))
                retry = run(core.handleSlashCommand({"command": "/agents retry daemon", "session": session, "config": config, "hooks": {}}))
            self.assertIn("daemon output task-daemon-fallback", log["message"])
            self.assertIn("[truncated to recent output]", log["message"])
            self.assertIn("Stopped task-daemon-fallback", stop["message"])
            self.assertIn("new task     task-daemon-retry", retry["message"])
            self.assertEqual(calls.count(("close",)), 3)
            self.assertIn(("read", "task-daemon-fallback", 12000), calls)
            self.assertIn(("stop", "task-daemon-fallback"), calls)
            self.assertIn(("retry", "task-daemon-fallback"), calls)
        finally:
            self.restore_home(root, previous)

    def test_shell_remote_collaboration_transcript_diagnostics_and_debug_commands(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            session["messages"] = [
                {"role": "user", "content": "render the transcript"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pytest"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "tests passed"}]},
            ]
            session["metadata"]["compactionState"] = {
                "latestCompactionGeneration": 2,
                "workingContextTokenEstimate": 1200,
                "workingContextTokenMeasurement": {"source": "fixture"},
                "compactedThroughMessageIndex": 1,
                "artifacts": {
                    "summary": str(workspace / "summary.md"),
                    "state": str(workspace / "state.json"),
                    "workingContext": str(workspace / "working-context.json"),
                    "checkpoint": str(workspace / "checkpoint.json"),
                },
            }
            (workspace / "summary.md").write_text("summary", encoding="utf-8")
            (workspace / "state.json").write_text("{}", encoding="utf-8")
            (workspace / "working-context.json").write_text("{}", encoding="utf-8")
            (workspace / "checkpoint.json").write_text("{}", encoding="utf-8")
            run(core.saveSession(session))
            run(core.saveCompactionManifest({"sessionId": session["id"], "latestGeneration": 2, "status": "ready"}))
            run(core.saveCompactionCheckpoint({"sessionId": session["id"], "generation": 2, "state": "ok"}))
            remote = run(core.createRemoteSession({"session": session, "description": "Shell remote", "channelServers": ["local"]}))
            bridge_runtime = run(core.createBridgeRuntime({"session": session, "remoteSessionId": remote["id"], "name": "shell-bridge", "startPolling": False}))
            bridge = bridge_runtime["bridge"]
            session["metadata"]["remoteSessionId"] = remote["id"]
            session["metadata"]["bridgeId"] = bridge["id"]
            run(core.saveSession(session))
            run(core.saveTaskRecord({"id": "task-collab-shell", "sessionId": session["id"], "kind": "background_command", "status": "running", "title": "Collaboration task", "updatedAt": "2026-01-01T00:00:00Z"}))
            run(core.saveMonitor(core.createMonitorRecord({"sessionId": session["id"], "taskId": "task-collab-shell", "goal": "Watch shell collaboration"})))
            run(core.saveChildAgent({"agentId": "agent-collab-shell", "sessionId": "child-collab-shell", "parentSessionId": session["id"], "name": "Collab worker", "provider": "openai", "model": "gpt-5.4", "mode": "auto", "runInBackground": True, "status": "awaiting_input", "startedAt": "2026-01-01T00:00:00Z"}))
            session["metadata"]["childAgentIds"] = ["agent-collab-shell"]

            transcript = run(core.handleSlashCommand({"command": "/transcript", "session": session, "config": config, "hooks": {}}))
            self.assertIn("render the transcript", transcript["message"])
            self.assertIn("tool         Bash", transcript["message"])
            self.assertIn("tests passed", transcript["message"])
            compact_status = run(core.handleSlashCommand({"command": "/compact status", "session": session, "config": config, "hooks": {}}))
            self.assertIn("latest gen     2", compact_status["message"])
            self.assertIn(str(workspace / "summary.md"), compact_status["message"])
            remote_list = run(core.handleSlashCommand({"command": "/remote list", "session": session, "config": config, "hooks": {}}))
            self.assertIn(remote["id"][:8], remote_list["message"])
            remote_inspect = run(core.handleSlashCommand({"command": f"/remote inspect {remote['id'][:8]}", "session": session, "config": config, "hooks": {}}))
            self.assertIn("Shell remote", remote_inspect["message"])
            queued = run(core.handleSlashCommand({"command": "/remote queue Continue from bridge", "session": session, "config": config, "hooks": {}}))
            self.assertIn("Queued", queued["message"])
            tail = run(core.handleSlashCommand({"command": f"/remote tail {bridge['id'][:8]}", "session": session, "config": config, "hooks": {}}))
            self.assertIn("bridge.message.queued", tail["message"])
            collaboration = run(core.handleSlashCommand({"command": "/collaboration", "session": session, "config": config, "hooks": {}}))
            self.assertIn("awaiting       1", collaboration["message"])
            self.assertIn("bridge", collaboration["message"])
            diagnostics = run(core.handleSlashCommand({"command": "/diagnostics", "session": session, "config": config, "hooks": {}}))
            self.assertIn("healthy", diagnostics["message"])
            self.assertIn("extensions", diagnostics["message"])
            debug_dir = workspace / "debug-bundle"
            debug = run(core.handleSlashCommand({"command": f"/debug {debug_dir}", "session": session, "config": config, "hooks": {"persist": lambda: None}}))
            self.assertIn(str(debug_dir), debug["message"])
            self.assertTrue((debug_dir / "manifest.json").exists())
            self.assertTrue((debug_dir / "remote" / "bridge.events.json").exists())
            compact_debug = run(core.handleSlashCommand({"command": f"/compact debug {workspace / 'compact-debug'}", "session": session, "config": config, "hooks": {"persist": lambda: None}}))
            self.assertIn("compaction    yes", compact_debug["message"])
            closed = run(core.handleSlashCommand({"command": f"/remote close {remote['id'][:8]}", "session": session, "config": config, "hooks": {"persist": lambda: None}}))
            self.assertIn("Closed", closed["message"])
            self.assertEqual(run(core.loadBridge(bridge["id"]))["status"], "closed")
            self.assertEqual(run(core.loadRemoteSession(remote["id"]))["bridgeStatus"], "closed")
            run(bridge_runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_shell_background_and_remote_attach_match_reference_selection(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            target = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="default")
            run(core.saveSession(target))
            run(
                core.saveTaskRecord(
                    {
                        "id": "task-bg-shell",
                        "sourceId": "task-source",
                        "sessionId": session["id"],
                        "kind": "background_command",
                        "status": "running",
                        "title": "Shell background task",
                        "summary": "streaming output",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:01Z",
                        "foregroundable": True,
                        "restorable": True,
                        "attachableSessionId": target["id"],
                        "command": "pytest",
                    }
                )
            )
            run(
                core.saveChildAgent(
                    {
                        "agentId": "agent-bg-shell",
                        "sessionId": target["id"],
                        "parentSessionId": session["id"],
                        "name": "Shell worker",
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "mode": "auto",
                        "runInBackground": True,
                        "status": "backgrounded",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "lastSummary": "working",
                    }
                )
            )
            session["metadata"]["childAgentIds"] = ["agent-bg-shell"]
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": "task-bg-shell", "goal": "Watch shell background"})
            run(core.saveMonitor(monitor))
            remote = run(core.createRemoteSession({"session": session, "description": "Remote Shell", "channelServers": ["local"]}))
            remote = run(
                core.updateRemoteSession(
                    remote["id"],
                    {
                        "status": "awaiting_input",
                        "foregroundSessionId": target["id"],
                        "lastEventName": "bridge.message.queued",
                        "lastEventSummary": "Input is queued",
                    },
                )
            )
            session["metadata"]["remoteSessionId"] = remote["id"]

            infos = []
            errors = []
            resumed = []
            counters = {"persist": 0}
            hooks = {
                "info": lambda title, message: infos.append((title, message)),
                "error": lambda message: errors.append(message),
                "persist": lambda: counters.__setitem__("persist", counters["persist"] + 1),
                "requestResume": lambda next_session: resumed.append(next_session["id"]),
            }

            background = run(core.handleSlashCommand({"command": "/background", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("tasks          1/1", background["message"])
            self.assertIn("workers        1/1", background["message"])
            self.assertIn("monitors       1/1", background["message"])
            self.assertIn("Remote:", background["message"])
            self.assertIn("tip            /background inspect <id> or /background attach <id>", background["message"])

            inspected_task = run(core.handleSlashCommand({"command": "/background inspect task-bg", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("id           task-bg-shell", inspected_task["message"])
            self.assertIn("attach       /tasks attach task-bg-shell", inspected_task["message"])
            inspected_agent = run(core.handleSlashCommand({"command": "/background inspect agent-bg", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Shell worker", inspected_agent["message"])
            inspected_monitor = run(core.handleSlashCommand({"command": f"/background inspect {monitor['id'][:8]}", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Watch shell background", inspected_monitor["message"])

            run(core.handleSlashCommand({"command": "/background attach task-bg-shell", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": f"/remote attach {remote['id'][:8]}", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(resumed, [target["id"], target["id"]])
            self.assertEqual(counters["persist"], 2)
            remote_report = run(core.handleSlashCommand({"command": "/remote", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Remote Shell", remote_report["message"])
            self.assertIn("foreground", remote_report["message"])

            monitor_attach = run(core.handleSlashCommand({"command": f"/background attach {monitor['id']}", "session": session, "config": config, "hooks": hooks}))
            self.assertTrue(monitor_attach["isError"])
            self.assertIn("No attachable session", errors[-1])
        finally:
            self.restore_home(root, previous)

    def test_shell_agents_and_plans_match_reference_reports(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            config["agents"] = {"reviewer": {"description": "Strict review pass"}}
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            target = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="default")
            run(core.saveSession(target))
            child = {
                "agentId": "agent-plan-shell",
                "sessionId": target["id"],
                "parentSessionId": session["id"],
                "name": "Plan worker",
                "provider": "openai",
                "model": "gpt-5.4",
                "mode": "auto",
                "runInBackground": True,
                "status": "awaiting_plan_approval",
                "startedAt": "2026-01-01T00:00:00Z",
                "taskId": "task-agent-plan",
                "attachableSessionId": target["id"],
                "waitingReason": "Review the proposed migration plan.",
                "retryable": True,
                "stoppable": True,
            }
            run(core.saveChildAgent(child))
            session["metadata"]["childAgentIds"] = [child["agentId"]]
            run(
                core.savePlanApproval(
                    {
                        "requestId": "plan-pending-shell",
                        "sessionId": target["id"],
                        "parentSessionId": session["id"],
                        "agentId": child["agentId"],
                        "status": "pending",
                        "createdAt": "2026-01-01T00:00:01Z",
                        "filePath": str(workspace / "plan.md"),
                        "plan": "Migrate shell command reports to parity.",
                        "allowedPrompts": [{"tool": "Bash", "prompt": "python3 -m unittest discover -s tests"}],
                    }
                )
            )
            run(
                core.savePlanApproval(
                    {
                        "requestId": "plan-approved-shell",
                        "sessionId": session["id"],
                        "status": "approved",
                        "createdAt": "2026-01-01T00:00:02Z",
                        "resolvedAt": "2026-01-01T00:00:03Z",
                        "plan": "Approved plan",
                        "allowedPrompts": [],
                    }
                )
            )

            resumed = []
            stopped = []
            retried = []
            counters = {"persist": 0}
            hooks = {
                "info": lambda *_: None,
                "error": lambda *_: None,
                "persist": lambda: counters.__setitem__("persist", counters["persist"] + 1),
                "requestResume": lambda next_session: resumed.append(next_session["id"]),
            }
            supervisor = {
                "stopTask": lambda task_id: stopped.append(task_id) or True,
                "retryTask": lambda task_id: retried.append(task_id) or {"taskId": "task-agent-retry", "outputFile": str(workspace / "agent.log")},
            }

            agents_summary = run(core.handleSlashCommand({"command": "/agents", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("reviewer", agents_summary["message"])
            self.assertIn("Strict review pass", agents_summary["message"])
            self.assertIn("child runs   1", agents_summary["message"])
            self.assertIn("awaiting     1", agents_summary["message"])
            agents_running = run(core.handleSlashCommand({"command": "/agents running", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Plan worker", agents_running["message"])
            self.assertIn("awaiting_plan_approval", agents_running["message"])
            inspected = run(core.handleSlashCommand({"command": "/agents inspect plan-shell", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("waiting      Review the proposed migration plan.", inspected["message"])

            run(core.handleSlashCommand({"command": "/agents attach agent-plan", "session": session, "config": config, "hooks": hooks}))
            stop = run(core.handleSlashCommand({"command": "/agents stop agent-plan", "session": session, "config": config, "hooks": hooks, "taskSupervisor": supervisor}))
            retry = run(core.handleSlashCommand({"command": "/agents retry agent-plan", "session": session, "config": config, "hooks": hooks, "taskSupervisor": supervisor}))
            self.assertEqual(resumed, [target["id"]])
            self.assertEqual(stopped, ["task-agent-plan"])
            self.assertEqual(retried, ["task-agent-plan"])
            self.assertIn("Stopped Plan worker", stop["message"])
            self.assertIn("new task     task-agent-retry", retry["message"])

            plans_pending = run(core.handleSlashCommand({"command": "/plans", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("current state  inactive", plans_pending["message"])
            self.assertIn("plan-pending", plans_pending["message"])
            self.assertIn("agent agent-plan", plans_pending["message"])
            self.assertIn("Bash(python3 -m unittest discover -s tests)", plans_pending["message"])
            plans_approved = run(core.handleSlashCommand({"command": "/plans approved", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("plan-approve", plans_approved["message"])
            plans_missing = run(core.handleSlashCommand({"command": "/plans missing-query", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("No plan approvals matched: missing-query", plans_missing["message"])
        finally:
            self.restore_home(root, previous)
