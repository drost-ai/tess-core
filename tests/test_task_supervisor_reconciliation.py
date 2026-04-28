from __future__ import annotations

from tests.support.agent_core_case import *


class TaskSupervisorReconciliationTests(AgentCoreTestCase):
    def test_reconcile_marks_lost_process_without_overwriting_detail_status(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "task-lost-after-restart"
            created_at = "2026-01-01T00:00:00Z"
            output_file = core.taskOutputPath(task_id)
            meta = {
                "type": "background_command",
                "taskId": task_id,
                "id": task_id,
                "sessionId": "session-lost",
                "command": "sleep",
                "cwd": str(workspace),
                "startedAt": created_at,
                "updatedAt": created_at,
                "status": "running",
                "outputFile": output_file,
                "detailStatus": "launching",
                "owner": "daemon",
            }
            run(core.writeTaskMeta(task_id, meta))
            run(
                core.saveTaskRecord(
                    {
                        "id": task_id,
                        "sessionId": "session-lost",
                        "sourceId": task_id,
                        "kind": "background_command",
                        "status": "running",
                        "title": "sleep",
                        "summary": "Background command is running.",
                        "createdAt": created_at,
                        "updatedAt": created_at,
                        "startedAt": created_at,
                        "cwd": str(workspace),
                        "command": "sleep",
                        "outputFile": output_file,
                        "foregroundMode": "background",
                        "taskFamily": "shell",
                        "detailStatus": "launching",
                        "retrieveOnly": True,
                        "owner": "daemon",
                    }
                )
            )
            events = []
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["detailStatus"], "launching")
            self.assertEqual(record["summary"], "Daemon restarted while this task was active and the process is no longer running.")
            next_meta = run(core.readTaskMeta(task_id))
            self.assertEqual(next_meta["status"], "failed")
            self.assertEqual(next_meta["type"], "background_command")
            self.assertEqual(next_meta["taskId"], task_id)
            self.assertEqual(next_meta["detailStatus"], "launching")
            failed_event = next(event for event in events if event["event"] == "task.failed" and event["data"]["task_id"] == task_id)
            self.assertEqual(failed_event["data"]["detail_status"], "launching")
            self.assertNotIn("updated_at", failed_event["data"])
        finally:
            if supervisor:
                run(supervisor["close"]())
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_reconcile_recovers_completed_subagent_metadata_after_restart(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "subagent-recovered-task"
            agent_id = "subagent-recovered-agent"
            child_session_id = "subagent-recovered-session"
            parent_session_id = "parent-recovered-session"
            output_file = core.taskOutputPath(task_id)
            proc = subprocess.Popen([sys.executable, "-c", ""], cwd=str(workspace), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc.wait(timeout=2)
            completed_at = "2026-01-01T00:00:03Z"
            run(
                core.saveChildAgent(
                    {
                        "agentId": agent_id,
                        "sessionId": child_session_id,
                        "parentSessionId": parent_session_id,
                        "name": "Recovered worker",
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "mode": "bypassPermissions",
                        "runInBackground": True,
                        "status": "completed",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "completedAt": completed_at,
                        "latestResultAt": completed_at,
                        "lastUpdatedAt": completed_at,
                        "completionReason": "worker_completed",
                        "resultSummary": "Recovered result",
                        "lastSummary": "Recovered result",
                        "attachableSessionId": child_session_id,
                        "owner": "daemon",
                        "retryable": False,
                        "stoppable": False,
                    }
                )
            )
            meta = {
                "type": "subagent",
                "taskId": task_id,
                "id": task_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "sessionId": child_session_id,
                "session_id": child_session_id,
                "parent_session_id": parent_session_id,
                "cwd": str(workspace),
                "pid": proc.pid,
                "startedAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "status": "running",
                "outputFile": output_file,
                "title": "Recovered worker",
                "attachableSessionId": child_session_id,
                "detailStatus": "worker_running",
                "owner": "daemon",
                "supervisorId": "previous-supervisor",
            }
            run(core.writeTaskMeta(task_id, meta))
            run(
                core.saveTaskRecord(
                    {
                        "id": task_id,
                        "sessionId": parent_session_id,
                        "sourceId": agent_id,
                        "kind": "subagent",
                        "status": "running",
                        "title": "Recovered worker",
                        "summary": "Background subagent is running under daemon supervision.",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "cwd": str(workspace),
                        "outputFile": output_file,
                        "foregroundMode": "background",
                        "taskFamily": "subagent",
                        "detailStatus": "worker_running",
                        "retrieveOnly": False,
                        "attachableSessionId": child_session_id,
                        "relatedChildSessionIds": [child_session_id],
                        "owner": "daemon",
                        "supervisorId": "previous-supervisor",
                        "metadata": {"agent_id": agent_id, "child_session_id": child_session_id, "pid": proc.pid},
                    }
                )
            )
            events = []
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["summary"], "Recovered result")
            self.assertEqual(record["detailStatus"], "worker_completed")
            self.assertEqual(record["metadata"]["recovery_state"], "completed_while_detached")
            next_meta = run(core.readTaskMeta(task_id))
            self.assertEqual(next_meta["status"], "completed")
            self.assertEqual(next_meta["completionReason"], "worker_completed")
            self.assertEqual(next_meta["resultSummary"], "Recovered result")
            child = run(core.loadChildAgent(agent_id))
            self.assertEqual(child["recoveryState"], "completed_while_detached")
            self.assertTrue(any(event["event"] == "task.completed" and event["data"]["task_id"] == task_id for event in events))
        finally:
            if supervisor:
                run(supervisor["close"]())
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_task_supervisor_reconciles_terminal_unknown_and_failed_subagent_states(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            completed_id = "terminal-main-after-restart"
            unknown_id = "unknown-pid-after-restart"
            failed_subagent_id = "failed-subagent-after-restart"
            agent_id = "failed-subagent-agent"
            parent_session_id = "failed-subagent-parent"
            child_session_id = "failed-subagent-child"
            created_at = "2026-01-01T00:00:00Z"
            completed_at = "2026-01-01T00:00:05Z"
            for task_id in (completed_id, unknown_id, failed_subagent_id):
                output_file = core.taskOutputPath(task_id)
                Path(output_file).parent.mkdir(parents=True, exist_ok=True)
                Path(output_file).write_text("", encoding="utf-8")
            run(core.writeTaskMeta(completed_id, {"type": "main_session", "taskId": completed_id, "sessionId": "terminal-session", "command": "continue", "cwd": str(workspace), "startedAt": created_at, "updatedAt": completed_at, "finishedAt": completed_at, "status": "completed", "outputFile": core.taskOutputPath(completed_id), "owner": "daemon"}))
            run(core.saveTaskRecord({"id": completed_id, "sessionId": "terminal-session", "sourceId": completed_id, "kind": "main_session", "status": "running", "title": "terminal", "summary": "Still running before restart.", "createdAt": created_at, "updatedAt": created_at, "startedAt": created_at, "cwd": str(workspace), "command": "continue", "outputFile": core.taskOutputPath(completed_id), "foregroundMode": "background", "taskFamily": "detached_session", "detailStatus": "session_starting", "retrieveOnly": False, "owner": "daemon"}))
            run(core.writeTaskMeta(unknown_id, {"type": "background_command", "taskId": unknown_id, "sessionId": "unknown-session", "command": "sleep", "cwd": str(workspace), "startedAt": created_at, "updatedAt": created_at, "status": "running", "outputFile": core.taskOutputPath(unknown_id), "owner": "daemon"}))
            run(core.saveTaskRecord({"id": unknown_id, "sessionId": "unknown-session", "sourceId": unknown_id, "kind": "background_command", "status": "running", "title": "unknown", "summary": "Background command is running.", "createdAt": created_at, "updatedAt": created_at, "startedAt": created_at, "cwd": str(workspace), "command": "sleep", "outputFile": core.taskOutputPath(unknown_id), "foregroundMode": "background", "taskFamily": "shell", "detailStatus": "launching", "retrieveOnly": True, "owner": "daemon"}))
            run(core.saveChildAgent({"agentId": agent_id, "sessionId": child_session_id, "parentSessionId": parent_session_id, "name": "Failed worker", "provider": "openai", "model": "gpt-5.4", "mode": "bypassPermissions", "runInBackground": True, "status": "failed", "startedAt": created_at, "completedAt": completed_at, "latestResultAt": completed_at, "lastUpdatedAt": completed_at, "completionReason": "worker_failed", "resultSummary": "Worker exploded", "lastSummary": "Worker exploded", "attachableSessionId": child_session_id, "owner": "daemon", "retryable": True, "stoppable": False}))
            run(core.writeTaskMeta(failed_subagent_id, {"type": "subagent", "taskId": failed_subagent_id, "task_id": failed_subagent_id, "agent_id": agent_id, "sessionId": child_session_id, "session_id": child_session_id, "parent_session_id": parent_session_id, "cwd": str(workspace), "pid": 999999999, "startedAt": created_at, "updatedAt": created_at, "status": "running", "outputFile": core.taskOutputPath(failed_subagent_id), "title": "Failed worker", "attachableSessionId": child_session_id, "detailStatus": "worker_running", "owner": "daemon"}))
            run(core.saveTaskRecord({"id": failed_subagent_id, "sessionId": parent_session_id, "sourceId": agent_id, "kind": "subagent", "status": "running", "title": "Failed worker", "summary": "Background subagent is running under daemon supervision.", "createdAt": created_at, "updatedAt": created_at, "startedAt": created_at, "cwd": str(workspace), "outputFile": core.taskOutputPath(failed_subagent_id), "foregroundMode": "background", "taskFamily": "subagent", "detailStatus": "worker_running", "retrieveOnly": False, "attachableSessionId": child_session_id, "relatedChildSessionIds": [child_session_id], "owner": "daemon", "metadata": {"agent_id": agent_id, "child_session_id": child_session_id, "pid": 999999999}}))

            events = []
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            completed = run(supervisor["inspectTask"](completed_id))
            unknown = run(supervisor["inspectTask"](unknown_id))
            failed_subagent = run(supervisor["inspectTask"](failed_subagent_id))
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["metadata"]["process_state"], "completed")
            self.assertEqual(unknown["status"], "failed")
            self.assertEqual(unknown["metadata"]["process_state"], "unknown")
            self.assertEqual(failed_subagent["status"], "failed")
            self.assertEqual(failed_subagent["summary"], "Worker exploded")
            self.assertEqual(failed_subagent["metadata"]["recovery_state"], "completed_while_detached")
            self.assertTrue(any(event["event"] == "task.completed" and event["data"]["task_id"] == completed_id for event in events))
            self.assertTrue(any(event["event"] == "task.failed" and event["data"]["task_id"] == failed_subagent_id for event in events))
            with self.assertRaises(RuntimeError) as missing_ctx:
                run(supervisor["startSubagentTask"]({"parentSessionId": parent_session_id, "sessionId": "missing-child-session", "agentId": "missing-agent", "cwd": str(workspace)}))
            self.assertIn("Subagent session not found: missing-child-session", str(missing_ctx.exception))
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_stop_task_marks_subagent_child_cancelled(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        proc = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "subagent-stop-task"
            agent_id = "subagent-stop-agent"
            child_session_id = "subagent-stop-session"
            parent_session_id = "parent-stop-session"
            output_file = core.taskOutputPath(task_id)
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(workspace)}
            if hasattr(os, "setsid"):
                kwargs["preexec_fn"] = os.setsid
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
            run(
                core.saveChildAgent(
                    {
                        "agentId": agent_id,
                        "sessionId": child_session_id,
                        "parentSessionId": parent_session_id,
                        "name": "Stopped worker",
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "mode": "bypassPermissions",
                        "runInBackground": True,
                        "status": "backgrounded",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "taskId": task_id,
                        "attachableSessionId": child_session_id,
                        "owner": "daemon",
                        "retryable": False,
                        "stoppable": True,
                    }
                )
            )
            meta = {
                "type": "subagent",
                "taskId": task_id,
                "id": task_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "sessionId": child_session_id,
                "session_id": child_session_id,
                "parent_session_id": parent_session_id,
                "cwd": str(workspace),
                "pid": proc.pid,
                "startedAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "status": "running",
                "outputFile": output_file,
                "title": "Stopped worker",
                "attachableSessionId": child_session_id,
                "detailStatus": "worker_running",
                "owner": "daemon",
            }
            run(core.writeTaskMeta(task_id, meta))
            run(
                core.saveTaskRecord(
                    {
                        "id": task_id,
                        "sessionId": parent_session_id,
                        "sourceId": agent_id,
                        "kind": "subagent",
                        "status": "running",
                        "title": "Stopped worker",
                        "summary": "Background subagent is running under daemon supervision.",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "cwd": str(workspace),
                        "outputFile": output_file,
                        "foregroundMode": "background",
                        "taskFamily": "subagent",
                        "detailStatus": "worker_running",
                        "retrieveOnly": False,
                        "attachableSessionId": child_session_id,
                        "relatedChildSessionIds": [child_session_id],
                        "owner": "daemon",
                        "metadata": {"agent_id": agent_id, "child_session_id": child_session_id, "pid": proc.pid},
                    }
                )
            )
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            self.assertTrue(run(supervisor["stopTask"](task_id)))
            child = run(core.loadChildAgent(agent_id))
            self.assertEqual(child["status"], "cancelled")
            self.assertEqual(child["completionReason"], "cancelled")
            self.assertFalse(child["stoppable"])
            self.assertTrue(child["retryable"])
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["status"], "stopped")
            self.assertEqual(meta["completionReason"], "cancelled")
            record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(record["status"], "stopped")
        finally:
            if supervisor:
                run(supervisor["close"]())
            if proc and proc.poll() is None:
                proc.terminate()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
            self.restore_home(root, previous)

    def test_task_stop_terminates_background_command(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            bash = next(tool for tool in core.createBuiltinTools() if tool.name == "Bash")
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(10)')}"
            events = []
            result = run(bash.handler({"command": command, "run_in_background": True}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "persistSession": lambda: None}))
            task_id = json.loads(result["content"])["task_id"]
            for _ in range(40):
                meta = run(core.readTaskMeta(task_id))
                if meta and isinstance(meta.get("pid"), int):
                    break
                time.sleep(0.05)
            self.assertEqual(events[0][1]["summary"], f"Run shell command in background\n{command}")
            stopper = next(tool for tool in core.createBuiltinTools() if tool.name == "TaskStop")
            stopped = run(stopper.handler({"task_id": task_id}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace)}))
            self.assertFalse(stopped.get("isError"))
            stopped_payload = json.loads(stopped["content"])
            self.assertEqual(stopped_payload["status"], "terminated")
            self.assertNotIn("stopped", stopped_payload)
            self.assertEqual(stopped_payload["execution"]["title"], f"Stopped background task {task_id}")
            self.assertEqual(stopped["meta"]["execution"]["status"], "terminated")
            for _ in range(20):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "terminated":
                    break
                time.sleep(0.05)
            self.assertEqual(run(core.readTaskMeta(task_id))["status"], "terminated")
            stop_permission = next(data for event, data in events if event == "permission.decided" and data["tool"] == "TaskStop")
            self.assertRegex(stop_permission["summary"], rf"^Stop background task {re.escape(task_id)} \(pid \d+\)$")
            self.assertEqual(events[-1][0], "task.failed")
            self.assertEqual(events[-1][1]["summary"], "Task terminated by user request.")
        finally:
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_task_stop_daemon_result_shape_matches_reference(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            stopper = next(tool for tool in core.createBuiltinTools() if tool.name == "TaskStop")

            async def stop_missing(_task_id):
                return False

            missing = run(
                stopper.handler(
                    {"task_id": "missing-task"},
                    {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "daemon": {"stopTask": stop_missing}},
                )
            )
            self.assertTrue(missing["isError"])
            self.assertEqual(missing["content"], "Task not found or not stoppable: missing-task")

            async def stop_found(_task_id):
                return True

            events = []
            stopped = run(
                stopper.handler(
                    {"task_id": "daemon-task"},
                    {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "daemon": {"stopTask": stop_found}},
                )
            )
            self.assertEqual(json.loads(stopped["content"]), {"task_id": "daemon-task", "status": "terminated"})
            self.assertNotIn("meta", stopped)
            self.assertEqual(events[-1], ("task.failed", {"session_id": session["id"], "task_id": "daemon-task", "summary": "Task terminated by user request."}))
        finally:
            self.restore_home(root, previous)
